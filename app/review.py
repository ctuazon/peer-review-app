"""Build review prompts and format Claude output for copy/paste."""
from __future__ import annotations

from typing import Any, Callable

from app.claude_runner import run_claude
from app.github_pr import PullRequestDiff, fetch_pull_request, summarize_diff_for_prompt
from app.prompts_store import Prompt
from app.wsl_auth import resolve_github_token

SYSTEM_OUTPUT_CONTRACT = """
You are performing a peer code review of a GitHub pull request.

Output rules (strict):
1. Review ONLY concrete changed lines from the diff. Do not invent files or line numbers.
2. Prefer comments on added lines (RIGHT side). Mention removed lines only when the deletion itself is the issue.
3. Output MUST be a sequence of copy-paste ready review comments in this exact format:

---
FILE: <path>
LINE: <number>
SIDE: RIGHT
SEVERITY: blocker|major|minor|nit|praise
COMMENT:
<one or more paragraphs of review feedback>
---

4. Put a blank line after each COMMENT block before the next --- separator.
5. If there are no issues, still emit at least one entry summarizing that the change looks good, using SEVERITY: praise on a representative changed line.
6. Keep each COMMENT focused and actionable so it can be pasted into a GitHub review.
7. Do not wrap the whole response in markdown code fences.

COMMENT writing style (important):
- Write like a real engineer on the team leaving a PR note. Stay technical and concrete.
- Lead with the problem, then why it matters, then a specific fix when useful.
- Do not use em dashes (—) or en dashes (–). Prefer commas, periods, colons, or parentheses.
- Avoid AI-sounding cadence and filler: "It's worth noting", "Notably", "Furthermore", "Additionally", "This ensures", "robust", "leverage", "comprehensive", "delve", stacked asides, or praise-then-critique templates.
- Prefer short, direct sentences over long balanced essays. Contractions are fine ("doesn't", "I'd").
- Skip throat-clearing and softener fluff. Say what you mean in plain engineering English.
""".strip()


# Cap how much prior-pass text is fed back so follow-ups don't blow the context.
MAX_PRIOR_PASSES = 3
MAX_PRIOR_CHARS = 40_000

FOLLOW_UP_RULES = """
This is a FOLLOW-UP review pass. The findings from earlier passes on this same PR are listed above.

Follow-up rules (strict, these override "review everything"):
1. Do NOT repeat, rephrase, or re-rank any issue already raised in a previous pass, even if it is still unfixed. Treat it as already reported.
2. Only report NEW issues with SEVERITY: blocker or major. Do not emit minor or nit entries on a follow-up pass.
3. If there are no new blocker/major issues, emit exactly ONE entry with SEVERITY: praise on a representative changed line saying no new blocking issues were found.
""".strip()


def _format_prior_reviews(prior_reviews: list[str]) -> str:
    """Newest-first list of past review outputs -> one bounded prompt section."""
    chunks: list[str] = []
    used = 0
    for index, text in enumerate(prior_reviews[:MAX_PRIOR_PASSES], start=1):
        text = text.strip()
        if not text:
            continue
        remaining = MAX_PRIOR_CHARS - used
        if remaining <= 0:
            break
        if len(text) > remaining:
            text = text[:remaining] + "\n…(truncated)"
        chunks.append(f"=== Previous pass {index} ({'most recent' if index == 1 else 'older'}) ===\n{text}")
        used += len(text)
    return "\n\n".join(chunks)


def build_review_prompt(
    *,
    reviewer_prompt: Prompt | None,
    story: str,
    diff: PullRequestDiff,
    prior_reviews: list[str] | None = None,
) -> str:
    prompt_body = (reviewer_prompt.content if reviewer_prompt else "").strip()
    prompt_name = reviewer_prompt.name if reviewer_prompt else "Ad-hoc"
    story_text = story.strip() or "(No story/context provided.)"

    prior_text = _format_prior_reviews(prior_reviews or [])
    follow_up = (
        f"""
Findings already reported in previous review passes of this PR:
{prior_text}

{FOLLOW_UP_RULES}
"""
        if prior_text
        else ""
    )

    # SYSTEM_OUTPUT_CONTRACT is passed separately (see run_peer_review) so it
    # can be cached instead of being rebilled as input tokens on every call.
    return f"""Reviewer prompt name: {prompt_name}
Reviewer prompt:
{prompt_body or "(No specialized reviewer prompt selected. Use sound general engineering judgment.)"}

Story / acceptance criteria / context from the requester:
{story_text}

Pull request under review:
{summarize_diff_for_prompt(diff)}
{follow_up}"""


def run_peer_review(
    *,
    pr_url: str,
    story: str,
    reviewer_prompt: Prompt | None,
    config: dict[str, Any],
    on_claude_event: Callable[[dict[str, str]], None] | None = None,
    prior_reviews: list[str] | None = None,
) -> tuple[PullRequestDiff, str]:
    token, auth_source = resolve_github_token(
        explicit_token=config.get("github_token") or "",
        use_wsl=bool(config.get("use_wsl_github_auth", True)),
    )
    if on_claude_event:
        on_claude_event({"kind": "status", "text": "fetching pull request"})
    diff = fetch_pull_request(pr_url, token=token)
    if on_claude_event:
        on_claude_event(
            {
                "kind": "status",
                "text": f"loaded {len(diff.files)} files — asking Claude",
            }
        )
    if on_claude_event and prior_reviews:
        count = min(len(prior_reviews), MAX_PRIOR_PASSES)
        on_claude_event(
            {
                "kind": "status",
                "text": f"follow-up pass: including {count} previous review(s), new blocker/major only",
            }
        )
    prompt = build_review_prompt(
        reviewer_prompt=reviewer_prompt,
        story=story,
        diff=diff,
        prior_reviews=prior_reviews,
    )
    review = run_claude(prompt, config, on_event=on_claude_event, system=SYSTEM_OUTPUT_CONTRACT)
    # Stash auth source on the diff object for UI status (non-serialized helper).
    setattr(diff, "auth_source", auth_source)
    return diff, review.strip()


EXPLAIN_PR_PROMPT = (
    "Explain this PR to me and its purpose. "
    "Cover what changed, why it likely exists, and the practical impact. "
    "Write clearly for a teammate who has not read the diff yet."
)


def build_explain_prompt(*, story: str, diff: PullRequestDiff) -> str:
    story_text = story.strip() or "(No extra story/explanation details provided.)"
    return f"""You are explaining a GitHub pull request to a teammate.

Hidden instruction (follow this):
{EXPLAIN_PR_PROMPT}

Use the requester's story/explanation details below when present — they may clarify intent, scope, or questions to answer.

Story / explanation details from the requester:
{story_text}

Pull request to explain:
{summarize_diff_for_prompt(diff)}

Output a clear plain-language explanation with short sections:
1. Purpose
2. What changed
3. Why it matters
4. Anything notable / risks

Do not invent files or behavior that are not supported by the PR content.
Do not wrap the whole response in markdown code fences.
""".strip()


def run_pr_explanation(
    *,
    pr_url: str,
    story: str,
    config: dict[str, Any],
    on_claude_event: Callable[[dict[str, str]], None] | None = None,
) -> tuple[PullRequestDiff, str]:
    token, auth_source = resolve_github_token(
        explicit_token=config.get("github_token") or "",
        use_wsl=bool(config.get("use_wsl_github_auth", True)),
    )
    if on_claude_event:
        on_claude_event({"kind": "status", "text": "fetching pull request"})
    diff = fetch_pull_request(pr_url, token=token)
    if on_claude_event:
        on_claude_event(
            {
                "kind": "status",
                "text": f"loaded {len(diff.files)} files — asking Claude to explain",
            }
        )
    prompt = build_explain_prompt(story=story, diff=diff)
    explanation = run_claude(prompt, config, on_event=on_claude_event)
    setattr(diff, "auth_source", auth_source)
    return diff, explanation.strip()


def build_ask_prompt(*, story: str, diff: PullRequestDiff) -> str:
    """Freeform ask: the story box is the only instruction (no review/explain templates)."""
    story_text = story.strip()
    return f"""You are helping a teammate with a GitHub pull request.

The requester's message below is your only instruction — follow it exactly.
Do not run a peer review unless they ask for one.
Do not force a canned PR explanation unless they ask for one.

Requester message:
{story_text}

Pull request context (use only as needed to answer the requester):
{summarize_diff_for_prompt(diff)}

Answer clearly in plain language.
Do not invent files or behavior that are not supported by the PR content.
Do not wrap the whole response in markdown code fences unless the requester asks for code.
""".strip()


def run_pr_ask(
    *,
    pr_url: str,
    story: str,
    config: dict[str, Any],
    on_claude_event: Callable[[dict[str, str]], None] | None = None,
) -> tuple[PullRequestDiff, str]:
    story_text = story.strip()
    if not story_text:
        raise ValueError(
            "Ask Claude needs text in the Story / explanation / custom prompt box."
        )
    token, auth_source = resolve_github_token(
        explicit_token=config.get("github_token") or "",
        use_wsl=bool(config.get("use_wsl_github_auth", True)),
    )
    if on_claude_event:
        on_claude_event({"kind": "status", "text": "fetching pull request"})
    diff = fetch_pull_request(pr_url, token=token)
    if on_claude_event:
        on_claude_event(
            {
                "kind": "status",
                "text": f"loaded {len(diff.files)} files — asking Claude",
            }
        )
    prompt = build_ask_prompt(story=story_text, diff=diff)
    answer = run_claude(prompt, config, on_event=on_claude_event)
    setattr(diff, "auth_source", auth_source)
    return diff, answer.strip()


def format_copy_friendly(review_text: str) -> str:
    """Normalize separators so each comment block is easy to copy."""
    text = review_text.replace("\r\n", "\n").strip()
    if not text:
        return ""
    # Ensure blocks start on their own lines.
    if not text.startswith("---"):
        text = "---\n" + text
    return text + ("\n" if not text.endswith("\n") else "")
