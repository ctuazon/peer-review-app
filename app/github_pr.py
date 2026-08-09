"""Fetch pull request metadata and diffs from GitHub."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import requests

PR_URL_RE = re.compile(
    r"https?://(?:www\.)?github\.com/"
    r"(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)",
    re.IGNORECASE,
)


@dataclass
class PullRequestRef:
    owner: str
    repo: str
    number: int
    url: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def repo_slug(self) -> str:
        return self.repo.lower()


@dataclass
class ChangedLine:
    file_path: str
    line: int | None
    side: str  # RIGHT (additions) or LEFT (deletions)
    content: str
    change_type: str  # added | removed | context


@dataclass
class PullRequestDiff:
    ref: PullRequestRef
    title: str
    body: str
    author: str
    base_branch: str
    head_branch: str
    files: list[dict[str, Any]] = field(default_factory=list)
    patch_text: str = ""
    changed_lines: list[ChangedLine] = field(default_factory=list)


def parse_pr_url(url: str) -> PullRequestRef:
    match = PR_URL_RE.search(url.strip())
    if not match:
        raise ValueError(
            "Invalid PR URL. Expected something like "
            "https://github.com/org/repo/pull/1234"
        )
    return PullRequestRef(
        owner=match.group("owner"),
        repo=match.group("repo"),
        number=int(match.group("number")),
        url=url.strip(),
    )


def _headers(token: str) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "peer-review-app",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _parse_patch(file_path: str, patch: str) -> list[ChangedLine]:
    lines: list[ChangedLine] = []
    old_line = 0
    new_line = 0

    for raw in patch.splitlines():
        if raw.startswith("@@"):
            # @@ -a,b +c,d @@
            m = re.search(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
            if m:
                old_line = int(m.group(1))
                new_line = int(m.group(2))
            continue

        if raw.startswith("\\"):
            continue

        if raw.startswith("+") and not raw.startswith("+++"):
            lines.append(
                ChangedLine(
                    file_path=file_path,
                    line=new_line,
                    side="RIGHT",
                    content=raw[1:],
                    change_type="added",
                )
            )
            new_line += 1
        elif raw.startswith("-") and not raw.startswith("---"):
            lines.append(
                ChangedLine(
                    file_path=file_path,
                    line=old_line,
                    side="LEFT",
                    content=raw[1:],
                    change_type="removed",
                )
            )
            old_line += 1
        elif raw.startswith(" "):
            old_line += 1
            new_line += 1

    return lines


def probe_pull_request(url: str, token: str = "") -> tuple[PullRequestRef, str]:
    """
    Lightweight connectivity check for a PR URL.
    Returns (ref, title) on success; raises on auth/network/not-found errors.
    """
    ref = parse_pr_url(url)
    headers = _headers(token)
    base = f"https://api.github.com/repos/{ref.owner}/{ref.repo}"
    pr_resp = requests.get(f"{base}/pulls/{ref.number}", headers=headers, timeout=30)
    if pr_resp.status_code == 401:
        raise PermissionError(
            "GitHub authentication failed. Use GitHub login in Settings."
        )
    if pr_resp.status_code == 404:
        raise FileNotFoundError(
            "PR not found (or no access). Check the URL / GitHub login."
        )
    pr_resp.raise_for_status()
    pr = pr_resp.json()
    return ref, pr.get("title") or ""


def fetch_pull_request(url: str, token: str = "") -> PullRequestDiff:
    ref = parse_pr_url(url)
    headers = _headers(token)
    base = f"https://api.github.com/repos/{ref.owner}/{ref.repo}"

    pr_resp = requests.get(f"{base}/pulls/{ref.number}", headers=headers, timeout=60)
    if pr_resp.status_code == 401:
        raise PermissionError(
            "GitHub authentication failed. Set a personal access token in Settings."
        )
    if pr_resp.status_code == 404:
        raise FileNotFoundError(
            "PR not found. Check the URL, or add a GitHub token for private repos."
        )
    pr_resp.raise_for_status()
    pr = pr_resp.json()

    files: list[dict[str, Any]] = []
    page = 1
    while True:
        files_resp = requests.get(
            f"{base}/pulls/{ref.number}/files",
            headers=headers,
            params={"per_page": 100, "page": page},
            timeout=60,
        )
        files_resp.raise_for_status()
        batch = files_resp.json()
        if not batch:
            break
        files.extend(batch)
        if len(batch) < 100:
            break
        page += 1

    changed_lines: list[ChangedLine] = []
    patch_chunks: list[str] = []
    for f in files:
        path = f.get("filename", "unknown")
        patch = f.get("patch") or ""
        status = f.get("status", "modified")
        header = f"=== {path} ({status}) ==="
        patch_chunks.append(header)
        if patch:
            patch_chunks.append(patch)
            changed_lines.extend(_parse_patch(path, patch))
        else:
            patch_chunks.append("(binary or too large to include patch)")

    return PullRequestDiff(
        ref=ref,
        title=pr.get("title") or "",
        body=pr.get("body") or "",
        author=(pr.get("user") or {}).get("login") or "",
        base_branch=(pr.get("base") or {}).get("ref") or "",
        head_branch=(pr.get("head") or {}).get("ref") or "",
        files=files,
        patch_text="\n".join(patch_chunks),
        changed_lines=changed_lines,
    )


def summarize_diff_for_prompt(diff: PullRequestDiff, max_chars: int = 120_000) -> str:
    file_list = "\n".join(
        f"- {f.get('filename')} (+{f.get('additions', 0)} / -{f.get('deletions', 0)}) "
        f"[{f.get('status', 'modified')}]"
        for f in diff.files
    )
    header = (
        f"PR: {diff.ref.url}\n"
        f"Title: {diff.title}\n"
        f"Author: {diff.author}\n"
        f"Branches: {diff.head_branch} -> {diff.base_branch}\n"
        f"Description:\n{diff.body or '(none)'}\n\n"
        f"Changed files:\n{file_list or '(none)'}\n\n"
        f"Unified diff:\n"
    )
    remaining = max_chars - len(header)
    patch = diff.patch_text
    if remaining > 0 and len(patch) > remaining:
        patch = patch[:remaining] + "\n\n[diff truncated due to size]"
    return header + patch
