"""A local checkout of the PR head so Claude's Read/Grep/Glob see the real repo.

Shallow-fetches the head commit with pygit2 into
data/checkouts/<owner>/<repo>/<sha>, writes the tree to disk, then deletes
secret-bearing files (.env*, *.pem, …) before Claude ever runs in it. A
checkout is reused while it exists and swept after `KEEP_DAYS`.
"""
from __future__ import annotations

import shutil
import stat
import time
from collections.abc import Callable
from pathlib import Path

from app import DATA_DIR
from app.diff_select import is_secret_path
from app.github_pr import PullRequestRef

try:
    import pygit2
except ImportError:  # pragma: no cover
    pygit2 = None  # type: ignore[assignment]

CHECKOUTS_DIR = DATA_DIR / "checkouts"
KEEP_DAYS = 7
_READY = ".peer-review-ready"


class CheckoutError(RuntimeError):
    pass


def _redact(text: str, token: str) -> str:
    return text.replace(token, "***") if token else text


def _on_rm_error(func, path, _exc):  # noqa: ANN001 -- shutil.rmtree onerror signature
    # Git pack files are read-only on Windows.
    Path(path).chmod(stat.S_IWRITE)
    func(path)


def remove_tree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, onerror=_on_rm_error)


def checkout_path(ref: PullRequestRef, sha: str) -> Path:
    return CHECKOUTS_DIR / ref.owner / ref.repo / sha


def strip_secret_files(root: Path) -> list[str]:
    removed: list[str] = []
    for path in root.rglob("*"):
        if ".git" in path.relative_to(root).parts or not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if is_secret_path(rel):
            path.unlink(missing_ok=True)
            removed.append(rel)
    return removed


def sweep_checkouts(keep_days: int = KEEP_DAYS) -> int:
    if not CHECKOUTS_DIR.exists():
        return 0
    cutoff = time.time() - keep_days * 86_400
    removed = 0
    for owner in CHECKOUTS_DIR.iterdir():
        for repo in owner.iterdir() if owner.is_dir() else []:
            for sha_dir in repo.iterdir() if repo.is_dir() else []:
                if sha_dir.stat().st_mtime < cutoff:
                    remove_tree(sha_dir)
                    removed += 1
    return removed


def ensure_checkout(
    ref: PullRequestRef,
    sha: str,
    token: str,
    on_status: Callable[[str], None] | None = None,
) -> Path:
    """Path to a ready checkout of `sha`; fetches it on first use."""
    if pygit2 is None:
        raise CheckoutError("pygit2 is not installed, so the repo can't be checked out (pip install pygit2).")
    if not sha:
        raise CheckoutError("The PR head SHA is unknown.")
    target = checkout_path(ref, sha)
    if (target / _READY).exists():
        target.touch()
        return target
    remove_tree(target)
    target.mkdir(parents=True, exist_ok=True)
    if on_status:
        on_status(f"checking out {ref.full_name}@{sha[:7]}")
    try:
        repo = pygit2.init_repository(str(target), bare=False)
        url = f"https://x-access-token:{token}@github.com/{ref.owner}/{ref.repo}.git" if token else (
            f"https://github.com/{ref.owner}/{ref.repo}.git"
        )
        remote = repo.remotes.create_anonymous(url)
        try:
            remote.fetch([sha], depth=1)
        except pygit2.GitError as exc:
            if "shallow" not in str(exc).lower():
                raise
            remote.fetch([sha])
        commit = repo.get(sha)
        if commit is None:
            raise CheckoutError(f"Fetched, but commit {sha[:12]} is missing.")
        repo.checkout_tree(commit.peel(pygit2.Tree), strategy=pygit2.GIT_CHECKOUT_FORCE)
        repo.set_head(commit.id)
    except CheckoutError:
        remove_tree(target)
        raise
    except Exception as exc:  # noqa: BLE001 -- libgit2 errors are varied
        remove_tree(target)
        raise CheckoutError(_redact(f"Couldn't check out {sha[:12]}: {exc}", token)) from exc
    removed = strip_secret_files(target)
    if on_status and removed:
        on_status(f"removed {len(removed)} secret-bearing file(s) from the checkout")
    (target / _READY).write_text(sha, encoding="utf-8")
    return target
