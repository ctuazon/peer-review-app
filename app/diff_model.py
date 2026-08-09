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
