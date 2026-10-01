"""Parse Claude review comment blocks into structured objects."""
from __future__ import annotations

import re
from dataclasses import dataclass

# "praise" only ever comes from the legacy text format; the JSON schema has no
# such severity, because a clean PR gets an approval and no findings.
LEGACY_SEVERITIES = ("blocker", "major", "minor", "nit", "praise")


@dataclass
class ReviewComment:
    """One finding. `comment` is the body; the rest is filled by the JSON path
    (title, suggestion, …) or set later by validation and posting."""

    file_path: str
    line: int | None
    side: str
    severity: str
    comment: str
    title: str = ""
    start_line: int | None = None
    symbol: str = ""
    anchor: str = ""
    suggestion: str | None = None
    confidence: str = "medium"
    also_flagged_by: str | None = None
    also_flagged_url: str | None = None
    fp: str = ""
    # Why this finding can't be posted inline as-is ("line not in diff",
    # "unknown severity 'x'"); empty when it's fine.
    problem: str = ""
    include: bool = True
    posted_url: str = ""
    verifier_note: str = ""

    @property
    def location(self) -> str:
        return self.file_path if self.line is None else f"{self.file_path}:{self.line}"

    @property
    def copy_block(self) -> str:
        line = self.line if self.line is not None else "?"
        return (
            f"---\n"
            f"FILE: {self.file_path}\n"
            f"LINE: {line}\n"
            f"SIDE: {self.side}\n"
            f"SEVERITY: {self.severity}\n"
            f"COMMENT:\n{self.comment.strip()}\n"
            f"---"
        )


# Each block starts at a FILE: line. Header fields after it may come in any
# order, and the model sometimes folds line/side into FILE itself
# ("FILE: a.php:48 · RIGHT" or "FILE: a.php:48 (RIGHT)").
_FILE_RE = re.compile(r"(?im)^[ \t]*FILE:[ \t]*(?P<rest>.*?)[ \t]*$")
_FIELD_RE = re.compile(r"(?i)^\s*(?P<key>LINE|SIDE|SEVERITY)\s*:\s*(?P<value>.*?)\s*$")
_COMMENT_RE = re.compile(r"(?i)^\s*COMMENT\s*:\s*(?P<inline>.*)$")
_INLINE_LOC_RE = re.compile(
    r"^(?P<path>\S+?):(?P<line>\d+)"
    r"(?:[\s·•|,\-(]*(?P<side>RIGHT|LEFT)\)?)?\s*$",
    re.IGNORECASE,
)
_SEPARATOR_RE = re.compile(r"^\s*-{3,}\s*$")


def _parse_block(file_rest: str, body: str) -> ReviewComment | None:
    path = file_rest.strip().strip("`")
    line: int | None = None
    side = ""
    severity = ""
    problems: list[str] = []

    loc = _INLINE_LOC_RE.match(path)
    if loc:
        path = loc.group("path")
        line = int(loc.group("line"))
        side = loc.group("side") or ""
    if not path or any(ch.isspace() for ch in path):
        return None

    lines = body.split("\n")
    idx = 0
    comment_lines: list[str] = []
    while idx < len(lines):
        raw = lines[idx]
        field = _FIELD_RE.match(raw)
        if field:
            key, value = field.group("key").upper(), field.group("value")
            if key == "LINE":
                line = int(value) if value.isdigit() else None
                if line is None and value and value != "?":
                    problems.append(f"LINE '{value}' is not a number")
            elif key == "SIDE" and value.upper() in ("RIGHT", "LEFT"):
                side = value
            elif key == "SEVERITY" and value:
                severity = value.split()[0]
            idx += 1
            continue
        comment = _COMMENT_RE.match(raw)
        if comment:
            if comment.group("inline").strip():
                comment_lines.append(comment.group("inline"))
            idx += 1
            break
        if not raw.strip():
            idx += 1
            continue
        # No COMMENT: marker; treat the first non-field line as the body.
        break
    comment_lines.extend(lines[idx:])

    while comment_lines and (not comment_lines[-1].strip() or _SEPARATOR_RE.match(comment_lines[-1])):
        comment_lines.pop()

    severity = severity.lower()
    if severity not in LEGACY_SEVERITIES:
        # Don't quietly downgrade an unknown severity to nit; keep it visible
        # but out of the submit set until someone picks a real one.
        problems.append(f"unknown severity '{severity}'" if severity else "no SEVERITY given")
    return ReviewComment(
        file_path=path.removeprefix("./"),
        line=line,
        side=(side or "RIGHT").upper(),
        severity=severity if severity in LEGACY_SEVERITIES else "nit",
        comment="\n".join(comment_lines).strip(),
        problem="; ".join(problems),
        include=not problems,
    )


def parse_review_comments(review_text: str) -> list[ReviewComment]:
    text = (review_text or "").replace("\r\n", "\n").strip()
    if not text:
        return []

    headers = list(_FILE_RE.finditer(text))
    comments: list[ReviewComment] = []
    for i, header in enumerate(headers):
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        parsed = _parse_block(header.group("rest"), text[header.end():end].lstrip("\n"))
        if parsed is not None:
            comments.append(parsed)
    return comments


def comments_to_copy_text(comments: list[ReviewComment]) -> str:
    if not comments:
        return ""
    return "\n\n".join(c.copy_block for c in comments) + "\n"


def build_verification_prompt(
    *,
    comments: list[ReviewComment],
    pr_url: str = "",
    pr_title: str = "",
    story: str = "",
    code_snippets: dict[str, str] | None = None,
) -> str:
    """
    Build a prompt asking another LLM to confirm whether review findings are real issues.
    """
    if not comments:
        return ""

    findings = comments_to_copy_text(comments).strip()
    snippets = code_snippets or {}
    snippet_blocks: list[str] = []
    for path in sorted(snippets):
        body = (snippets[path] or "").strip()
        if not body:
            continue
        snippet_blocks.append(f"### {path}\n```diff\n{body}\n```")

    story_text = (story or "").strip() or "(none provided)"
    context = "\n\n".join(snippet_blocks) if snippet_blocks else "(no code snippets available)"

    return f"""You are validating a peer code review. Decide whether each finding is a real issue, a false positive, or not actionable.

For every finding below, respond with:
- VERDICT: real_issue | false_positive | needs_more_context | nit_only
- CONFIDENCE: high | medium | low
- WHY: one short paragraph
- SUGGESTED ACTION: keep | drop | soften | rewrite

Only judge from the provided PR context and code snippets. Do not invent files or lines.

PR: {pr_url or '(unknown)'}
Title: {pr_title or '(unknown)'}

Story / acceptance criteria:
{story_text}

Review findings:
{findings}

Relevant code snippets:
{context}
""".strip() + "\n"
