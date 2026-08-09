"""Parse Claude review comment blocks into structured objects."""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class ReviewComment:
    file_path: str
    line: int | None
    side: str
    severity: str
    comment: str

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


_BLOCK_RE = re.compile(
    r"FILE:\s*(?P<file>.+?)\s*\n"
    r"LINE:\s*(?P<line>\d+|\?)\s*\n"
    r"(?:SIDE:\s*(?P<side>RIGHT|LEFT)\s*\n)?"
    r"(?:SEVERITY:\s*(?P<severity>\w+)\s*\n)?"
    r"COMMENT:\s*\n(?P<comment>.*?)(?=\n---|\Z)",
    re.IGNORECASE | re.DOTALL,
)


def parse_review_comments(review_text: str) -> list[ReviewComment]:
    text = (review_text or "").replace("\r\n", "\n").strip()
    if not text:
        return []

    comments: list[ReviewComment] = []
    for match in _BLOCK_RE.finditer(text):
        line_raw = match.group("line").strip()
        line = int(line_raw) if line_raw.isdigit() else None
        comments.append(
            ReviewComment(
                file_path=match.group("file").strip().lstrip("./"),
                line=line,
                side=(match.group("side") or "RIGHT").strip().upper(),
                severity=(match.group("severity") or "nit").strip().lower(),
                comment=(match.group("comment") or "").strip(),
            )
        )
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
