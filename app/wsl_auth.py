"""Reuse GitHub auth already available inside WSL (git-credentials / keychain)."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlparse

from app.process_util import no_window_kwargs


class WslAuthError(RuntimeError):
    pass


def wsl_available() -> bool:
    return shutil.which("wsl") is not None


def _run_wsl(bash_command: str, timeout: int = 20) -> str:
    if not wsl_available():
        raise WslAuthError("WSL is not available on this machine.")
    completed = subprocess.run(
        ["wsl", "-e", "bash", "-lc", bash_command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        **no_window_kwargs(),
    )
    if completed.returncode != 0:
        err = (completed.stderr or completed.stdout or "").strip()
        raise WslAuthError(err or "WSL command failed.")
    return completed.stdout


def _token_from_git_credentials_line(line: str) -> str | None:
    line = line.strip()
    if not line or "github.com" not in line.lower():
        return None
    # Formats:
    # https://USER:TOKEN@github.com
    # https://TOKEN@github.com
    parsed = urlparse(line if "://" in line else f"https://{line}")
    if "github.com" not in (parsed.hostname or "").lower():
        return None
    if parsed.password:
        return unquote(parsed.password)
    if parsed.username and not parsed.password:
        # Some stores put the token in the username field.
        return unquote(parsed.username)
    match = re.search(r"https?://(?:[^/\s:@]+:)?([^/\s:@]+)@github\.com", line, re.I)
    if match:
        return unquote(match.group(1))
    return None


def get_github_token_from_wsl() -> str:
    """
    Prefer ~/.git-credentials in WSL (already present for this machine).
    Falls back to `git credential fill` if needed.
    """
    # Direct read is fast and matches the existing setup.
    try:
        raw = _run_wsl(
            "grep -E 'github\\.com' ~/.git-credentials 2>/dev/null | head -n 5 || true"
        )
        for line in raw.splitlines():
            token = _token_from_git_credentials_line(line)
            if token:
                return token
    except (WslAuthError, subprocess.TimeoutExpired):
        pass

    # Fallback: ask git's credential helper (may use Windows GCM via WSL).
    fill_script = (
        "printf 'protocol=https\\nhost=github.com\\n\\n' | "
        "git credential fill 2>/dev/null | "
        "awk -F= '/^password=/{print $2; exit}'"
    )
    try:
        token = _run_wsl(fill_script, timeout=45).strip()
        if token:
            return token
    except (WslAuthError, subprocess.TimeoutExpired) as exc:
        raise WslAuthError(
            "Could not read GitHub credentials from WSL. "
            "Ensure ~/.git-credentials has a github.com entry, "
            "or set a token in Settings."
        ) from exc

    raise WslAuthError(
        "No GitHub token found in WSL. "
        "Add one to ~/.git-credentials or paste a PAT in Settings."
    )


def get_github_token_from_windows_gcm() -> str:
    """Ask Windows Git Credential Manager for a github.com password/token."""
    git = shutil.which("git") or r"C:\Program Files\Git\cmd\git.exe"
    if not os.path.isfile(git):
        raise WslAuthError("git.exe not found for Windows credential lookup.")
    env = os.environ.copy()
    git_dir = str(Path(git).parent)
    mingw = str(Path(git).resolve().parents[1] / "mingw64" / "bin")
    env["PATH"] = os.pathsep.join([git_dir, mingw, env.get("PATH", "")])
    try:
        completed = subprocess.run(
            [git, "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=45,
            check=False,
            env=env,
            **no_window_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WslAuthError(f"Windows credential lookup failed: {exc}") from exc
    if completed.returncode != 0:
        raise WslAuthError(
            (completed.stderr or completed.stdout or "GCM returned no credential").strip()
        )
    for line in (completed.stdout or "").splitlines():
        if line.startswith("password="):
            token = line.split("=", 1)[1].strip()
            if token:
                return token
    raise WslAuthError("Windows GCM did not return a GitHub password/token.")


def resolve_github_token(explicit_token: str = "", use_wsl: bool = True) -> tuple[str, str]:
    """
    Returns (token, source_label).
    Prefers an explicit app setting, then WSL git-credentials, then Windows GCM.
    """
    if explicit_token.strip():
        return explicit_token.strip(), "settings"
    if use_wsl:
        try:
            return get_github_token_from_wsl(), "wsl-git-credentials"
        except WslAuthError:
            pass
    try:
        return get_github_token_from_windows_gcm(), "windows-gcm"
    except WslAuthError as exc:
        raise WslAuthError(
            "No GitHub token found in app settings, WSL, or Windows Git Credential Manager. "
            "Use Settings → Login with GitHub (browser)."
        ) from exc
