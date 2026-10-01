"""Parse unified diffs into display rows for an inline review view."""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class DiffDisplayRow:
    kind: str  # hunk | context | added | removed | meta
    file_path: str
    old_line: int | None
    new_line: int | None
    text: str


def parse_patch_display_rows(file_path: str, patch: str) -> list[DiffDisplayRow]:
    rows: list[DiffDisplayRow] = []
    old_line = 0
    new_line = 0

    for raw in (patch or "").splitlines():
        if raw.startswith("@@"):
            m = re.search(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)", raw)
            if m:
                old_line = int(m.group(1))
                new_line = int(m.group(2))
                trailing = (m.group(3) or "").strip()
                label = raw if not trailing else f"@@ -{m.group(1)} +{m.group(2)} @@ {trailing}"
            else:
                label = raw
            rows.append(
                DiffDisplayRow(
                    kind="hunk",
                    file_path=file_path,
                    old_line=None,
                    new_line=None,
                    text=label,
                )
            )
            continue

        if raw.startswith("\\"):
            rows.append(
                DiffDisplayRow("meta", file_path, None, None, raw)
            )
            continue

        if raw.startswith("+") and not raw.startswith("+++"):
            rows.append(
                DiffDisplayRow(
                    kind="added",
                    file_path=file_path,
                    old_line=None,
                    new_line=new_line,
                    text=raw[1:],
                )
            )
            new_line += 1
        elif raw.startswith("-") and not raw.startswith("---"):
            rows.append(
                DiffDisplayRow(
                    kind="removed",
                    file_path=file_path,
                    old_line=old_line,
                    new_line=None,
                    text=raw[1:],
                )
            )
            old_line += 1
        elif raw.startswith(" ") or raw == "":
            content = raw[1:] if raw.startswith(" ") else raw
            rows.append(
                DiffDisplayRow(
                    kind="context",
                    file_path=file_path,
                    old_line=old_line,
                    new_line=new_line,
                    text=content,
                )
            )
            old_line += 1
            new_line += 1
        else:
            rows.append(DiffDisplayRow("meta", file_path, None, None, raw))

    return rows


def normalize_path(path: str) -> str:
    return (path or "").strip().lstrip("./").replace("\\", "/").lower()


# --- Hunk model: line validation and the line-numbered prompt diff ----------
# Port of the Laravel app's DiffParser / FileDiff. GitHub accepts an inline
# comment on RIGHT for added + context lines and on LEFT for deleted + context
# lines inside a hunk; anything else is a 422.

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class DiffLine:
    origin: str  # "+" | "-" | " "
    text: str
    old_line: int | None
    new_line: int | None


@dataclass
class DiffHunk:
    old_start: int
    new_start: int
    header: str
    lines: list[DiffLine]

    @property
    def old_end(self) -> int:
        """One past the last old-side line this hunk covers."""
        return self.old_start + sum(1 for ln in self.lines if ln.origin != "+")

    @property
    def new_end(self) -> int:
        return self.new_start + sum(1 for ln in self.lines if ln.origin != "-")


@dataclass
class FileDiff:
    path: str
    hunks: list[DiffHunk]
    previous_path: str | None = None
    is_binary: bool = False
    status: str = "modified"

    def commentable_lines(self, side: str) -> set[int]:
        left = side.upper() == "LEFT"
        out: set[int] = set()
        for hunk in self.hunks:
            for ln in hunk.lines:
                if left and ln.origin != "+" and ln.old_line is not None:
                    out.add(ln.old_line)
                elif not left and ln.origin != "-" and ln.new_line is not None:
                    out.add(ln.new_line)
        return out

    def accepts(self, line: int | None, side: str = "RIGHT") -> bool:
        return line is not None and line in self.commentable_lines(side)

    @property
    def changed_lines(self) -> int:
        """Added plus deleted lines, the size the review model is picked by."""
        return sum(1 for hunk in self.hunks for ln in hunk.lines if ln.origin in ("+", "-"))

    def right_lines(self, start: int, end: int) -> list[str] | None:
        """Text of RIGHT lines start..end, or None if any is outside the diff."""
        found: dict[int, str] = {}
        for hunk in self.hunks:
            for ln in hunk.lines:
                if ln.new_line is not None and start <= ln.new_line <= end and ln.origin != "-":
                    found[ln.new_line] = ln.text
        if len(found) != end - start + 1:
            return None
        return [found[n] for n in range(start, end + 1)]

    def enclosing_symbol(self, line: int) -> str | None:
        """The function/class git named in the header of the hunk holding `line`."""
        for hunk in self.hunks:
            numbers = [ln.new_line for ln in hunk.lines if ln.new_line is not None]
            if numbers and hunk.new_start <= line <= max(numbers):
                context = re.sub(r"^@@[^@]*@@", "", hunk.header).strip()
                return context or None
        return None

    def annotated(self) -> str:
        """The diff with the file line number against every line: `R12 +x`, `L40 -y`."""
        out = [f"--- {self.path}" + (f" (renamed from {self.previous_path})" if self.previous_path else "")]
        if self.is_binary:
            out.append("(binary file, no textual diff)")
        for hunk in self.hunks:
            out.append(hunk.header)
            for ln in hunk.lines:
                deleted = ln.origin == "-"
                number = ln.old_line if deleted else ln.new_line
                out.append(f"{'L' if deleted else 'R'}{number if number is not None else '':<5} {ln.origin}{ln.text}")
        return "\n".join(out)

    def map_forward(self, old_line: int) -> int | None:
        """Where old-side `old_line` sits on the new side, or None if deleted.

        Used to carry a comment's line through a compare diff (base..head of a
        re-review) so a thread anchored on the old commit can be matched.
        """
        offset = 0
        for hunk in self.hunks:
            if old_line < hunk.old_start:
                return old_line + offset
            if old_line < hunk.old_end:
                for ln in hunk.lines:
                    if ln.old_line == old_line:
                        return ln.new_line if ln.origin == " " else None
                return None
            offset = hunk.new_end - hunk.old_end
        return old_line + offset


def parse_file_patch(
    path: str, patch: str, *, previous_path: str | None = None, status: str = "modified"
) -> FileDiff:
    """Parse one GitHub `files[].patch` (hunks only, no `diff --git` header)."""
    hunks: list[DiffHunk] = []
    current: DiffHunk | None = None
    old_line = new_line = 0
    for raw in (patch or "").splitlines():
        if raw.startswith("@@"):
            m = _HUNK_RE.match(raw)
            if not m:
                current = None
                continue
            old_line, new_line = int(m.group(1)), int(m.group(3))
            current = DiffHunk(old_line, new_line, raw, [])
            hunks.append(current)
            continue
        if current is None or raw.startswith("\\"):
            continue
        origin = raw[:1] or " "
        if origin not in "+- ":
            continue
        text = raw[1:]
        current.lines.append(
            DiffLine(
                origin=origin,
                text=text,
                old_line=None if origin == "+" else old_line,
                new_line=None if origin == "-" else new_line,
            )
        )
        if origin != "+":
            old_line += 1
        if origin != "-":
            new_line += 1
    return FileDiff(
        path=path,
        hunks=hunks,
        previous_path=previous_path,
        is_binary=not patch,
        status=status,
    )


def build_file_diffs(files: list[dict]) -> dict[str, FileDiff]:
    """GitHub `pulls/{n}/files` (or `compare` files) -> {path: FileDiff}."""
    out: dict[str, FileDiff] = {}
    for f in files:
        path = f.get("filename") or ""
        if not path:
            continue
        out[path] = parse_file_patch(
            path,
            f.get("patch") or "",
            previous_path=f.get("previous_filename"),
            status=f.get("status") or "modified",
        )
    return out
