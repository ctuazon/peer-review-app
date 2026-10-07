"""Decide which files of a PR the model sees (port of DiffSelector + SecretGuard).

Secret-bearing files are withheld by name whatever they contain. Generated
and vendored files (lockfiles, vendor/, dist/, *.min.*) are skipped by
default. What's left is ordered hand-written source first, then tests, then
config, and cut at file boundaries when over budget: a file dropped whole and
named misleads less than one cut mid-hunk.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.diff_model import FileDiff
from app.globs import matches_any

SECRET_PATHS = [
    "**/.env",
    "**/.env.*",
    "**/*.pem",
    "**/*.key",
    "**/*.p12",
    "**/*.pfx",
    "**/*.tfvars",
    "**/id_rsa",
    "**/id_ed25519",
    "**/*.jks",
    "**/*.keystore",
]
GENERATED = [
    "**/*.lock",
    "**/*-lock.*",
    "**/*.min.*",
    "**/*.map",
    "**/*.snap",
    "**/vendor/**",
    "**/node_modules/**",
    "**/dist/**",
    "**/build/**",
    "**/generated/**",
    "**/*.generated.*",
]
TESTS = ["tests/**", "**/tests/**", "**/__tests__/**", "**/*Test.php", "**/*.spec.*", "**/*.test.*", "**/*_test.*", "**/test_*.py"]
CONFIG = ["config/**", "**/*.json", "**/*.yml", "**/*.yaml", "**/*.xml", "**/*.ini", "**/*.toml", "**/*.neon", "**/Dockerfile", "**/*.conf"]

# Code runs nearer three characters to a token than four; err on showing less.
CHARS_PER_TOKEN = 3
DEFAULT_MAX_CHARS = 900_000  # ~300K tokens of annotated diff; config `max_diff_chars` overrides it

WITHHELD_SECRET = "secret-bearing by name"
GENERATED_REASON = "generated or vendored"
EXCLUDED = "excluded by config"
OVER_BUDGET = "over the input budget"


@dataclass
class DiffSelection:
    shown: list[str] = field(default_factory=list)
    omitted: dict[str, str] = field(default_factory=dict)

    @property
    def withheld_secret(self) -> list[str]:
        return [p for p, why in self.omitted.items() if why == WITHHELD_SECRET]

    @property
    def over_budget(self) -> list[str]:
        return [p for p, why in self.omitted.items() if why == OVER_BUDGET]


def is_secret_path(path: str) -> bool:
    return matches_any(SECRET_PATHS, path)


def _rank(path: str) -> int:
    if matches_any(GENERATED, path):
        return 3
    if matches_any(TESTS, path):
        return 1
    if matches_any(CONFIG, path):
        return 2
    return 0


def select_diffs(
    diffs: dict[str, FileDiff],
    *,
    exclude_globs: list[str] | None = None,
    skip_generated: bool = True,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> DiffSelection:
    selection = DiffSelection()
    candidates: list[str] = []
    for path in diffs:
        if is_secret_path(path):
            selection.omitted[path] = WITHHELD_SECRET
        elif exclude_globs and matches_any(exclude_globs, path):
            selection.omitted[path] = EXCLUDED
        elif skip_generated and matches_any(GENERATED, path):
            selection.omitted[path] = GENERATED_REASON
        else:
            candidates.append(path)

    candidates.sort(key=_rank)  # stable: PR order within a rank
    spent = 0
    for index, path in enumerate(candidates):
        size = len(diffs[path].annotated()) + 2
        if spent + size > max_chars:
            for dropped in candidates[index:]:
                selection.omitted[dropped] = OVER_BUDGET
            break
        spent += size
        selection.shown.append(path)
    return selection


def _not_shown(selection: DiffSelection) -> str:
    if not selection.omitted:
        return ""
    rows = "\n".join(f"- {p} ({why})" for p, why in selection.omitted.items())
    return (
        "\n\n# Not shown\n\nThese files changed but their diff is not above. If you can read the "
        "repository, read one when a finding depends on it; otherwise do not guess about it. "
        "Never read or quote a file withheld as secret-bearing.\n\n" + rows
    )


def render_selected(diffs: dict[str, FileDiff], selection: DiffSelection) -> str:
    """Annotated diff of the shown files, plus a 'not shown' list."""
    parts = [diffs[p].annotated() for p in selection.shown]
    text = "\n\n".join(parts) if parts else "(no reviewable diff)"
    return text + _not_shown(selection)


def review_delta(
    since_diffs: dict[str, FileDiff] | None, diffs: dict[str, FileDiff], selection: DiffSelection
) -> dict[str, FileDiff] | None:
    """On a re-review, what changed since the last reviewed commit in the
    files the review would show, or None to send the full diff instead (the
    old commit is gone, or the delta is no smaller, e.g. after a base merge)."""
    if since_diffs is None:
        return None
    delta = {p: since_diffs[p] for p in selection.shown if p in since_diffs}
    delta_size = sum(len(d.annotated()) for d in delta.values())
    full_size = sum(len(diffs[p].annotated()) for p in selection.shown)
    return delta if delta_size < full_size else None


def render_delta(delta: dict[str, FileDiff], selection: DiffSelection, since_sha: str) -> str:
    """The re-review diff: only the changes since `since_sha`."""
    unchanged = [p for p in selection.shown if p not in delta]
    text = (
        f"Only what changed since {since_sha[:7]}, the commit you last reviewed, is shown; the rest of "
        "the pull request was reviewed then. `L` numbers here are against that commit, so anchor new "
        "findings to `R` lines only.\n\n"
        + ("\n\n".join(d.annotated() for d in delta.values()) or "(none of the reviewed files changed)")
    )
    if unchanged:
        text += "\n\n# Unchanged since your last review\n\n" + "\n".join(f"- {p}" for p in unchanged)
    return text + _not_shown(selection)
