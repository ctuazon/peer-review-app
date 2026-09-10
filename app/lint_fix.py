"""Scan a PR's changed files for lint/style-type issues on the lines the PR
actually touches, draft fixes with Claude, and let the user review/apply
them as one batch commit.

Same drafted-fix -> self-review -> human-approval pipeline the bot-comment
flow uses (app/bot_review.py) -- here Claude is finding the issues itself
rather than triaging someone else's comment, and only ever looks at lines
this PR's own diff changed, so it can't turn into unrelated repo-wide
reformatting.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from app.bot_review import BOT_REVIEW_SYSTEM, FileFix, unified_diff_for
from app.claude_runner import run_claude
from app.github_pr import (
    PullRequestDiff,
    commit_multiple_files,
    fetch_pull_request,
    get_authenticated_login,
    get_file_content,
)
from app.wsl_auth import resolve_github_token

EventCallback = Callable[[dict[str, str]], None]

# Files with no textual patch (binary, too large) or that this PR deleted
# have nothing for a lint pass to look at.
_SKIP_STATUSES = {"removed"}


@dataclass
class LintFinding:
    path: str
    issues: list[str] = field(default_factory=list)
    fix: FileFix | None = None
    safe: bool = True
    safety_notes: str = ""


def _lintable_files(diff: PullRequestDiff) -> list[dict[str, Any]]:
    return [
        f
        for f in diff.files
        if f.get("filename") and f.get("patch") and f.get("status") not in _SKIP_STATUSES
    ]


def build_lint_prompt(*, diff: PullRequestDiff, path: str, patch: str, content: str) -> str:
    return f"""Review ONLY the lines this pull request actually changed in
{path} for lint/style-type issues -- the kind a linter or formatter would
flag: unused imports/variables, obviously dead code, inconsistent
formatting/naming versus the rest of this same file, missing or incorrect
language-idiomatic patterns, and similar mechanical issues. Do NOT flag
pre-existing issues in lines this PR didn't touch, and do NOT suggest
architectural or behavioral changes -- lint-style only.

Pull request: {diff.ref.url}
Title: {diff.title}

Diff for {path} (only +/- lines are part of this PR's change; everything
else is unchanged context):
```
{patch}
```

Full current content of {path} on branch {diff.head_branch}:
```
{content}
```

If you find no lint issues on the changed lines, output exactly:
ISSUES: none

Otherwise output exactly:
ISSUES:
- <line number>: <short description of the issue>
(one bullet per issue, only for lines this PR changed)
FIX_CONTENT:
<the ENTIRE corrected file content, verbatim, no markdown fences, no
truncation -- fixing only the listed issues, nothing else>
"""


_ISSUES_NONE_RE = re.compile(r"(?im)^ISSUES:\s*none\s*$")
_ISSUES_BLOCK_RE = re.compile(
    r"ISSUES:\s*\n(?P<body>.*?)(?=\nFIX_CONTENT:|\Z)", re.DOTALL | re.IGNORECASE
)
_FIX_CONTENT_RE = re.compile(r"FIX_CONTENT:\s*\n", re.IGNORECASE)


def parse_lint_response(text: str) -> tuple[list[str], str | None]:
    """Returns (issue_descriptions, fixed_content). fixed_content is None if
    no issues were found, or the model reported issues without a usable fix."""
    text = (text or "").strip()
    if _ISSUES_NONE_RE.search(text):
        return [], None

    issues: list[str] = []
    issues_match = _ISSUES_BLOCK_RE.search(text)
    if issues_match:
        for line in issues_match.group("body").splitlines():
            line = line.strip().lstrip("-").strip()
            if line:
                issues.append(line)

    fix_match = _FIX_CONTENT_RE.search(text)
    if not fix_match or not issues:
        return issues, None
    return issues, text[fix_match.end():].strip("\n")


def build_lint_self_review_prompt(*, path: str, patch: str, fix: FileFix) -> str:
    return f"""You proposed the lint fix below for {path}. Nobody else will
look at this before it's committed, so sanity-check your own work now.

Original diff for this PR (only +/- lines are part of this PR's change):
```
{patch}
```

Your proposed fix (diff against the file's current content):
```
{fix.diff_text}
```

Check specifically for: syntax errors, unbalanced brackets/quotes/parens,
broken imports/references, and whether the fix touches ANY line outside
what this PR's own diff above changed (it must not -- that would be
unrelated churn, not a lint fix).

Output exactly:
SAFE: yes|no
NOTES: <one short paragraph -- if SAFE is no, say exactly what's wrong>
"""


_SELF_REVIEW_RE = re.compile(
    r"SAFE:\s*(?P<safe>yes|no)\s*\nNOTES:\s*(?P<notes>.*)\Z", re.IGNORECASE | re.DOTALL
)


def parse_lint_self_review(text: str) -> tuple[bool, str]:
    match = _SELF_REVIEW_RE.search((text or "").strip())
    if not match:
        return False, "Could not parse the self-review output; treating the fix as unsafe."
    return (match.group("safe") or "").strip().lower() == "yes", (match.group("notes") or "").strip()


def run_lint_check(
    *,
    pr_url: str,
    config: dict[str, Any],
    on_event: EventCallback | None = None,
) -> tuple[PullRequestDiff, list[LintFinding], str]:
    """Returns (diff, findings, github_token) -- the token is needed by the
    caller to apply approved fixes once the user reviews each one."""
    token, auth_source = resolve_github_token(
        explicit_token=config.get("github_token") or "",
        use_wsl=bool(config.get("use_wsl_github_auth", True)),
    )
    if on_event:
        on_event({"kind": "status", "text": "fetching pull request"})
    diff = fetch_pull_request(pr_url, token=token)
    setattr(diff, "auth_source", auth_source)

    login = get_authenticated_login(token)
    if not login or login.lower() != (diff.author or "").lower():
        raise PermissionError(
            f"This PR is authored by '{diff.author or 'unknown'}', but you're signed "
            f"in to GitHub as '{login or 'unknown'}'. You can only run this check on "
            "your own PRs."
        )

    files = _lintable_files(diff)
    findings: list[LintFinding] = []
    for i, f in enumerate(files, start=1):
        path = f["filename"]
        if on_event:
            on_event({"kind": "status", "text": f"scanning {i}/{len(files)}: {path}"})
        try:
            content, _sha = get_file_content(diff.ref, path, diff.head_branch, token=token)
        except Exception:  # noqa: BLE001
            continue

        prompt = build_lint_prompt(diff=diff, path=path, patch=f.get("patch") or "", content=content)
        output = run_claude(prompt, config, on_event=on_event, system=BOT_REVIEW_SYSTEM)
        issues, fixed_content = parse_lint_response(output)
        if not issues:
            continue  # clean on the changed lines

        if fixed_content is None:
            findings.append(
                LintFinding(
                    path=path,
                    issues=issues,
                    fix=None,
                    safe=False,
                    safety_notes="The model reported issue(s) but didn't return a fix to apply.",
                )
            )
            continue

        fix = FileFix(
            path=path,
            new_content=fixed_content,
            commit_message=f"Fix lint issue(s) in {path}",
            original_content=content,
        )
        fix.diff_text = unified_diff_for(fix)
        if fix.diff_text == "(no textual difference detected)":
            continue  # issues named but nothing actually changed -- not a real finding

        if on_event:
            on_event({"kind": "status", "text": f"self-reviewing lint fix for {path}"})
        review_prompt = build_lint_self_review_prompt(path=path, patch=f.get("patch") or "", fix=fix)
        review_output = run_claude(review_prompt, config, on_event=on_event, system=BOT_REVIEW_SYSTEM)
        safe, notes = parse_lint_self_review(review_output)

        findings.append(LintFinding(path=path, issues=issues, fix=fix, safe=safe, safety_notes=notes))

    return diff, findings, token


def apply_lint_fixes_batch(
    diff: PullRequestDiff,
    findings: list[LintFinding],
    token: str,
) -> None:
    """Apply every fix from the given (already user-approved) findings as one
    atomic commit on the PR's head branch. Each finding is for a distinct
    file, so -- unlike the bot-comment flow -- there's no cross-finding
    reconciliation to do."""
    selected = [f for f in findings if f.fix is not None]
    if not selected:
        raise ValueError("None of the selected findings have a fix to apply.")

    files = {f.path: f.fix.new_content for f in selected}
    lines = [f"Fix {len(selected)} lint issue(s)", ""]
    for finding in selected:
        for issue in finding.issues:
            lines.append(f"- {finding.path}: {issue}")
    message = "\n".join(lines)

    commit_multiple_files(diff.ref, diff.head_branch, files, message, token=token)
