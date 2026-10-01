"""Post a reviewed set of findings to GitHub (port of ReviewPublisher,
ReviewSummary and ThreadResolver, minus the auto-posting).

Nothing here runs without the user clicking: the submit dialog decides which
findings go, edits the summary, and picks the event. Never APPROVE.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import requests

from app.diff_model import FileDiff
from app.github_pr import (
    PullRequestDiff,
    create_line_comment,
    create_pr_review,
    delete_review_comment,
    fetch_review_comments,
    post_issue_comment,
    reply_to_review_comment,
    resolve_review_thread,
    update_pr_review,
)
from app.review_parse import ReviewComment
from app.review_prompts import fix_prompt
from app.review_schema import (
    SEVERITIES,
    SEVERITY_LABELS,
    Disagreement,
    PriorStatus,
    ReviewResult,
    fp_marker,
    parse_fp_marker,
    parse_review_marker,
    review_marker,
)

GITHUB_BODY_LIMIT = 60_000
EVENTS = ("COMMENT", "REQUEST_CHANGES")
LOW_CONFIDENCE_NOTE = (
    "_I could not verify this against the surrounding code, treat it as a question rather than a defect._"
)


def comment_body(f: ReviewComment, head_sha: str) -> str:
    label = SEVERITY_LABELS.get(f.severity, f.severity.capitalize())
    title = f.title or f.comment.split("\n", 1)[0][:80]
    body = f"{fp_marker(f.fp, f.severity, head_sha)}\n**{label}: {title}**\n\n{f.comment.strip()}"
    if f.also_flagged_by:
        who = f"[{f.also_flagged_by}]({f.also_flagged_url})" if f.also_flagged_url else f.also_flagged_by
        body += f"\n\n_Also flagged by {who}._"
    if f.confidence == "low":
        body += "\n\n" + LOW_CONFIDENCE_NOTE
    if f.suggestion:
        body += f"\n\n```suggestion\n{f.suggestion.rstrip()}\n```"
    return body


def suggestion_allowed(f: ReviewComment, diff: FileDiff | None) -> bool:
    """A suggestion replaces the cited lines verbatim, so every one of them
    must be a RIGHT line in the diff."""
    if not f.suggestion or diff is None or f.side != "RIGHT" or f.line is None:
        return False
    return diff.right_lines(f.start_line or f.line, f.line) is not None


def inline_payload(f: ReviewComment, diffs: dict[str, FileDiff], head_sha: str) -> dict[str, Any] | None:
    """Review-comment payload, or None when the finding can't be anchored."""
    diff = diffs.get(f.file_path)
    if f.line is None or diff is None or not diff.accepts(f.line, f.side):
        return None
    shown = f
    if f.suggestion and not suggestion_allowed(f, diff):
        shown = ReviewComment(**{**f.__dict__, "suggestion": None})
    payload: dict[str, Any] = {
        "path": f.file_path,
        "line": f.line,
        "side": f.side,
        "body": comment_body(shown, head_sha),
    }
    if f.start_line is not None and f.start_line < f.line and diff.accepts(f.start_line, f.side):
        payload["start_line"] = f.start_line
        payload["start_side"] = f.side
    return payload


# --- summary -----------------------------------------------------------------

@dataclass
class SummaryInput:
    diff: PullRequestDiff
    result: ReviewResult
    findings: list[ReviewComment]  # the ones being posted
    ticket_summaries: list[tuple[str, str]] = field(default_factory=list)
    ticket_keys: list[str] = field(default_factory=list)
    withheld: list[str] = field(default_factory=list)
    prior: list[PriorStatus] = field(default_factory=list)
    since_sha: str = ""
    footer: str = ""
    include_fix_prompts: bool = True


def _finding_line(f: ReviewComment, inline: bool) -> str:
    header = f"**`{f.location}`**: {f.title or 'finding'}"
    if f.also_flagged_by:
        who = f"[{f.also_flagged_by}]({f.also_flagged_url})" if f.also_flagged_url else f.also_flagged_by
        header += f" _(also flagged by {who})_"
    if f.confidence == "low":
        header += " _(unverified)_"
    if f.posted_url:
        header += f" · [comment]({f.posted_url})"
    elif not inline:
        header += " _(not posted inline" + (f": {f.problem}" if f.problem else "") + ")_"
    body = f.comment.strip()
    if not inline and f.suggestion:
        body += f"\n\nSuggested change:\n```\n{f.suggestion.rstrip()}\n```"
    return f"{header}\n\n{body}"


def render_summary(data: SummaryInput, inline_ids: set[int] | None = None) -> str:
    inline_ids = inline_ids or set()
    r = data.result
    posted = data.findings
    blockers = sum(1 for f in posted if f.severity == "blocker")
    if r.verdict == "approve":
        headline = "Approve"
    elif r.verdict == "approve_with_nits":
        headline = "Approve with nits"
    else:
        headline = f"Changes requested: {blockers} blocker{'s' if blockers != 1 else ''}" if blockers else "Changes requested"
    sections: list[str] = [review_marker(data.diff.head_sha, data.diff.base_sha), f"## {headline}\n\n{r.verdict_reason}".rstrip()]

    if data.ticket_summaries:
        sections.append("**Ticket context**\n" + "\n".join(f"- **{k}**: {s}" for k, s in data.ticket_summaries))
    if r.scope_note:
        scope = f"**Scope reviewed**: {r.scope_note}"
        if data.withheld:
            scope += "\n\nNot shown to the model: `" + "`, `".join(data.withheld) + "`."
        sections.append(scope)
    if data.prior:
        icon = {"fixed": "✅", "open": "⏳", "withdrawn": "↩", "changed": "✏"}
        rows = []
        for p in data.prior:
            link = f" ([thread]({p.thread_url}))" if p.thread_url else ""
            rows.append(f"- {icon.get(p.status, '')} `{p.status}`: {p.title}{f' ({p.note})' if p.note else ''}{link}")
        sections.append("### Since my last review\n\n" + "\n".join(rows))

    if not posted:
        since = f"No new issues since `{data.since_sha[:7]}`." if data.since_sha else "Nothing to report at this depth."
        sections.append(f"### Findings\n\n{since}")
    else:
        parts = ["### Findings"]
        for sev in SEVERITIES:
            group = [f for f in posted if f.severity == sev]
            if group:
                parts.append(f"#### {SEVERITY_LABELS[sev]} ({len(group)})")
                parts += [_finding_line(f, id(f) in inline_ids) for f in group]
        sections.append("\n\n".join(parts))

    if r.credits or r.disagreements:
        rows = ["### Other reviewers"]
        for c in r.credits:
            who = f"[{c.reviewer}]({c.url})" if c.url else c.reviewer
            rows.append(f"- Credit to **{who}**: {c.point}")
        for d in r.disagreements:
            who = f"[{d.reviewer}]({d.url})" if d.url else d.reviewer
            rows.append(f"- I disagree with **{who}** on: {d.claim}\n\n  {d.rebuttal}")
        sections.append("\n".join(rows))
    if r.tests_note:
        sections.append(f"### Tests\n\n{r.tests_note}")
    if data.include_fix_prompts and posted:
        blocks = [f"```markdown\n{fix_prompt(f, data.ticket_keys)}\n```" for f in posted]
        sections.append(
            "<details>\n<summary>Fix prompts: one per finding, paste into an AI agent</summary>\n\n"
            "Each block is self-contained, including the handling rules.\n\n" + "\n\n".join(blocks) + "\n\n</details>"
        )
    if data.footer:
        sections.append(f"<sub>{data.footer}</sub>")
    return trim_body("\n\n".join(s for s in sections if s))


def trim_body(body: str) -> str:
    """GitHub rejects bodies over ~65K; drop fix prompts first, then cut."""
    if len(body) <= GITHUB_BODY_LIMIT:
        return body
    start = body.find("<details>\n<summary>Fix prompts")
    if start != -1:
        end = body.find("</details>", start)
        body = body[:start] + body[end + len("</details>") :] if end != -1 else body[:start]
        body = body.rstrip() + "\n\n<sub>Fix prompts were omitted: this review is too long for one GitHub comment.</sub>"
    if len(body) > GITHUB_BODY_LIMIT:
        body = body[: GITHUB_BODY_LIMIT - 40] + "\n\n…(truncated)"
    return body


def ensure_marker(body: str, diff: PullRequestDiff) -> str:
    """The review marker is what a later re-review finds; keep it even if the
    user edited the summary and deleted it."""
    return body if parse_review_marker(body) else review_marker(diff.head_sha, diff.base_sha) + "\n" + body


# --- posting -----------------------------------------------------------------

def already_posted(diff: PullRequestDiff, token: str) -> dict[str, str]:
    """{fp: comment url} for this app's inline comments on the current head."""
    out: dict[str, str] = {}
    try:
        comments = fetch_review_comments(diff.ref, token=token)
    except requests.RequestException:
        return out
    for c in comments:
        marker = parse_fp_marker(c.get("body") or "")
        if marker and marker.get("sha") == diff.head_sha:
            out.setdefault(marker["fp"], c.get("html_url") or "")
    return out


@dataclass
class PublishOutcome:
    review_url: str = ""
    review_id: int | None = None
    inline: int = 0
    one_by_one: int = 0
    skipped_existing: int = 0
    demoted: list[tuple[ReviewComment, str]] = field(default_factory=list)
    superseded: bool = False
    notes: list[str] = field(default_factory=list)


def _error_text(exc: requests.HTTPError) -> str:
    try:
        payload = exc.response.json()
        errors = payload.get("errors") or []
        return f"{exc.response.status_code}: {payload.get('message', '')} {errors}".strip()
    except Exception:  # noqa: BLE001
        return str(exc)


def publish_review(
    *,
    diff: PullRequestDiff,
    file_diffs: dict[str, FileDiff],
    findings: list[ReviewComment],
    summary_for: Callable[[set[int]], str],
    event: str,
    token: str,
    previous_review_id: int | None = None,
    previous_review_body: str = "",
    on_status: Callable[[str], None] | None = None,
) -> PublishOutcome:
    """One review with every anchorable finding inline and the summary as its
    body. `summary_for(inline_ids)` renders the body knowing which findings
    made it inline. If GitHub rejects the batch (a bad anchor), the comments
    go one by one and the summary follows, listing what was demoted."""
    if event not in EVENTS:
        raise ValueError(f"Unsupported review event {event!r}: this app never approves.")
    out = PublishOutcome()
    status = on_status or (lambda _t: None)

    existing = already_posted(diff, token)
    payloads: dict[int, dict[str, Any]] = {}
    for f in findings:
        if f.fp and f.fp in existing:
            f.posted_url = existing[f.fp]
            out.skipped_existing += 1
            continue
        payload = inline_payload(f, file_diffs, diff.head_sha)
        if payload is None:
            out.demoted.append((f, f.problem or "not tied to a diff line"))
        else:
            payloads[id(f)] = payload
    by_id = {id(f): f for f in findings}

    review: dict[str, Any] | None = None
    status(f"posting review with {len(payloads)} inline comment(s)")
    try:
        review = create_pr_review(
            diff.ref, commit_id=diff.head_sha, body=summary_for(set(payloads)), event=event,
            comments=list(payloads.values()), token=token,
        )
        out.inline = len(payloads)
    except requests.HTTPError as exc:
        if exc.response is None or exc.response.status_code != 422 or not payloads:
            raise
        out.notes.append(f"GitHub rejected the batched review ({_error_text(exc)}); posting comments one by one.")
        status("batched review rejected, posting comments one by one")
        posted_ids: set[int] = set()
        for key, payload in payloads.items():
            f = by_id[key]
            try:
                created = create_line_comment(diff.ref, {**payload, "commit_id": diff.head_sha}, token=token)
                f.posted_url = created.get("html_url") or ""
                posted_ids.add(key)
                out.one_by_one += 1
            except requests.HTTPError as one_exc:
                f.problem = f"GitHub rejected the anchor ({_error_text(one_exc)})"
                out.demoted.append((f, f.problem))
        review = create_pr_review(
            diff.ref, commit_id=diff.head_sha, body=summary_for(posted_ids), event=event, token=token
        )

    out.review_url = (review or {}).get("html_url") or ""
    out.review_id = (review or {}).get("id")
    for fp, url in already_posted(diff, token).items():
        for f in findings:
            if f.fp == fp and not f.posted_url:
                f.posted_url = url

    if previous_review_id and previous_review_id != out.review_id and out.review_url:
        out.superseded = mark_superseded(diff, previous_review_id, previous_review_body, out.review_url, token)
    return out


def mark_superseded(diff: PullRequestDiff, review_id: int, body: str, new_url: str, token: str) -> bool:
    marker = parse_review_marker(body)
    if not marker or "**Superseded.**" in body:
        return False
    banner = f"> **Superseded.** A newer review of this pull request is at {new_url}.\n\n"
    try:
        update_pr_review(diff.ref, review_id, body.replace(marker["marker"], marker["marker"] + "\n\n" + banner, 1), token=token)
        return True
    except requests.RequestException:
        return False


def amend_previous(diff: PullRequestDiff, review_id: int, new_url: str, token: str) -> int:
    """Delete this app's older inline comments (other commits) and point the
    old review at the new one. Destructive, so only on an explicit opt-in."""
    removed = 0
    try:
        comments = fetch_review_comments(diff.ref, token=token)
    except requests.RequestException:
        return 0
    for c in comments:
        marker = parse_fp_marker(c.get("body") or "")
        if marker and marker.get("sha") != diff.head_sha and c.get("id"):
            try:
                delete_review_comment(diff.ref, int(c["id"]), token=token)
                removed += 1
            except requests.RequestException:
                continue
    try:
        update_pr_review(
            diff.ref, review_id,
            review_marker(diff.head_sha, diff.base_sha) + f"\n\n> **Superseded.** Replaced by {new_url}.",
            token=token,
        )
    except requests.RequestException:
        pass
    return removed


def post_single(diff: PullRequestDiff, f: ReviewComment, file_diffs: dict[str, FileDiff], token: str) -> str:
    """One finding on its own: inline when anchorable, else a PR comment."""
    if f.fp and f.fp in already_posted(diff, token):
        raise ValueError("Already on the PR for this commit.")
    payload = inline_payload(f, file_diffs, diff.head_sha)
    if payload is None:
        label = SEVERITY_LABELS.get(f.severity, f.severity)
        created = post_issue_comment(
            diff.ref, f"{fp_marker(f.fp, f.severity, diff.head_sha)}\n**{label}: {f.title or 'finding'}** (`{f.location}`)\n\n{f.comment}",
            token=token,
        )
    else:
        created = create_line_comment(diff.ref, {**payload, "commit_id": diff.head_sha}, token=token)
    f.posted_url = created.get("html_url") or ""
    return f.posted_url


def post_rebuttal(diff: PullRequestDiff, d: Disagreement, token: str) -> str:
    if d.comment_id is None:
        raise ValueError("That comment isn't an inline thread, so it can't be replied to directly.")
    created = reply_to_review_comment(diff.ref, d.comment_id, d.rebuttal, token=token)
    d.posted_url = created.get("html_url") or ""
    return d.posted_url


def reply_and_resolve(diff: PullRequestDiff, p: PriorStatus, token: str, *, resolve: bool) -> str:
    """Reply on the thread; resolve it too when `resolve` (own threads by default)."""
    if p.comment_id is None:
        raise ValueError("No matching review thread was found for this finding.")
    sha7 = diff.head_sha[:7]
    if p.status == "withdrawn":
        text = f"Withdrawing this: {p.note}" if p.note else "Withdrawing this."
    elif resolve:
        text = f"Verified fixed in `{sha7}`" + (f": {p.note}" if p.note else ".")
    else:
        text = f"Looks addressed in `{sha7}`" + (f": {p.note}" if p.note else ".") + "\n\n_Leaving this thread for you to resolve._"
    reply_to_review_comment(diff.ref, p.comment_id, text, token=token)
    if resolve and p.thread_id:
        resolve_review_thread(p.thread_id, token=token)
        return "replied and resolved"
    return "replied"
