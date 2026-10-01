"""Pre-run checks that warn before tokens are spent (port of TriggerConfig).

These are warnings with a "Run anyway" escape, never hard blocks: the desktop
is driven by a person who can decide a draft is worth a look.
"""
from __future__ import annotations

import re

from app.globs import matches
from app.repo_config import DEFAULT_IGNORE_KEYWORDS, RepoConfig


def title_carries(title: str, keyword: str) -> bool:
    """Whole word, hyphens part of the word: 'WIP' matches 'WIP: x' but not
    'Wipe cache' or 'WIP-1234'."""
    keyword = keyword.strip()
    if not keyword:
        return False
    pattern = r"(?<![\w-])" + re.escape(keyword) + r"(?![\w-])"
    return re.search(pattern, title or "", re.IGNORECASE | re.UNICODE) is not None


def eligibility_warnings(
    *,
    title: str,
    draft: bool,
    state: str,
    merged: bool,
    base_branch: str,
    head_sha: str,
    last_reviewed_sha: str,
    cfg: RepoConfig,
) -> list[str]:
    warnings: list[str] = []
    if merged:
        warnings.append("This PR is already merged.")
    elif state and state != "open":
        warnings.append(f"This PR is {state}.")
    if draft:
        warnings.append("This PR is a draft.")
    keywords = cfg.ignore_title_keywords if cfg.ignore_title_keywords is not None else DEFAULT_IGNORE_KEYWORDS
    for keyword in keywords:
        if title_carries(title, keyword):
            warnings.append(f'The title contains "{keyword}".')
            break
    if cfg.base_branches and not any(b == base_branch or matches(b, base_branch) for b in cfg.base_branches):
        warnings.append(f"It targets {base_branch}, which pr-review.yml doesn't list as a reviewed branch.")
    if head_sha and last_reviewed_sha and head_sha == last_reviewed_sha:
        warnings.append(f"No new commits since the last review (head is still {head_sha[:7]}).")
    return warnings
