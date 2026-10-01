"""Browser / SSO login helpers for GitHub and Claude."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from app.process_util import no_window_kwargs, start_hidden


def _gcm_path() -> str | None:
    candidates = [
        shutil.which("git-credential-manager"),
        r"C:\Program Files\Git\mingw64\bin\git-credential-manager.exe",
        r"C:\Program Files (x86)\Git\mingw64\bin\git-credential-manager.exe",
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def find_wsl_claude() -> str | None:
    """Locate the Claude Code binary inside WSL (VS Code / Cursor extension or PATH)."""
    if not shutil.which("wsl"):
        return None
    script = r"""
if command -v claude >/dev/null 2>&1; then
  command -v claude
  exit 0
fi
for base in "$HOME/.vscode-server/extensions" "$HOME/.cursor-server/extensions"; do
  if [ -d "$base" ]; then
    found=$(find "$base" -path '*/native-binary/claude' -type f 2>/dev/null | sort | tail -n 1)
    if [ -n "$found" ]; then
      printf '%s\n' "$found"
      exit 0
    fi
  fi
done
# Known Claude Code extension layout fallback
for candidate in "$HOME"/.vscode-server/extensions/anthropic.claude-code-*/resources/native-binary/claude \
                 "$HOME"/.cursor-server/extensions/anthropic.claude-code-*/resources/native-binary/claude; do
  if [ -f "$candidate" ]; then
    printf '%s\n' "$candidate"
    exit 0
  fi
done
exit 1
"""
    try:
        completed = subprocess.run(
            ["wsl", "-e", "bash", "-lc", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
            **no_window_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    path = (completed.stdout or "").strip().splitlines()
    return path[-1].strip() if path and completed.returncode == 0 else None


def clear_github_auth() -> list[str]:
    """Remove stored GitHub credentials from WSL and Windows GCM. Returns notes."""
    notes: list[str] = []

    # WSL ~/.git-credentials entries for github.com
    if shutil.which("wsl"):
        script = (
            "if [ -f ~/.git-credentials ]; then "
            "grep -vi 'github.com' ~/.git-credentials > ~/.git-credentials.tmp 2>/dev/null || true; "
            "mv ~/.git-credentials.tmp ~/.git-credentials; "
            "echo cleared_wsl; "
            "else echo no_wsl_file; fi"
        )
        try:
            completed = subprocess.run(
                ["wsl", "-e", "bash", "-lc", script],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
                **no_window_kwargs(),
            )
            out = (completed.stdout or "").strip()
            if "cleared_wsl" in out:
                notes.append("Cleared WSL git-credentials for github.com")
            elif "no_wsl_file" in out:
                notes.append("No WSL git-credentials file")
        except (OSError, subprocess.TimeoutExpired) as exc:
            notes.append(f"WSL credential clear failed: {exc}")

    # Windows Git Credential Manager accounts
    gcm = _gcm_path()
    if gcm:
        try:
            listed = subprocess.run(
                [gcm, "github", "list"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
                **no_window_kwargs(),
            )
            accounts = [
                line.strip()
                for line in (listed.stdout or "").splitlines()
                if line.strip() and not line.strip().lower().startswith("description")
            ]
            if not accounts:
                notes.append("No Windows GCM GitHub accounts listed")
            for account in accounts:
                # Account lines are typically just the username.
                account_name = account.split()[0]
                subprocess.run(
                    [gcm, "github", "logout", account_name],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=30,
                    check=False,
                    **no_window_kwargs(),
                )
                notes.append(f"Logged out Windows GCM account: {account_name}")
        except (OSError, subprocess.TimeoutExpired) as exc:
            notes.append(f"GCM logout failed: {exc}")

        # Also erase via git credential erase as a belt-and-suspenders step.
        git = shutil.which("git") or r"C:\Program Files\Git\cmd\git.exe"
        if os.path.isfile(git):
            try:
                subprocess.run(
                    [git, "credential", "reject"],
                    input="protocol=https\nhost=github.com\n\n",
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=20,
                    check=False,
                    **no_window_kwargs(),
                )
                notes.append("Sent GitHub credential reject to git")
            except (OSError, subprocess.TimeoutExpired):
                pass
    else:
        notes.append("Git Credential Manager not found")

    return notes


def clear_claude_auth() -> list[str]:
    """Log out Claude Code SSO session in WSL. Returns notes."""
    notes: list[str] = []
    claude = find_wsl_claude()
    if not claude:
        notes.append("Claude CLI not found in WSL")
        return notes
    try:
        completed = subprocess.run(
            ["wsl", "-e", "bash", "-lc", f'"{claude}" auth logout'],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
            **no_window_kwargs(),
        )
        if completed.returncode == 0:
            notes.append("Logged out Claude SSO session")
        else:
            err = (completed.stderr or completed.stdout or "").strip()
            notes.append(err or "Claude logout returned a non-zero exit code")
    except (OSError, subprocess.TimeoutExpired) as exc:
        notes.append(f"Claude logout failed: {exc}")
    return notes


def clear_all_auth() -> str:
    """Clear GitHub + Claude stored auth used by this app."""
    notes = clear_github_auth() + clear_claude_auth()
    return "\n".join(f"- {n}" for n in notes) if notes else "No auth stores were changed."


def start_github_web_login(*, force: bool = False) -> str:
    """
    Launch Git Credential Manager's browser login (opens a webpage).
    Runs hidden so no extra console sticks around on the taskbar.
    """
    gcm = _gcm_path()
    if not gcm:
        raise RuntimeError(
            "Git Credential Manager not found. Install Git for Windows, then try again."
        )
    cmd = [gcm, "github", "login", "--web"]
    if force:
        cmd.append("--force")
    start_hidden(cmd)
    return (
        "Opened GitHub browser login.\n"
        "Complete sign-in in your browser, then click Refresh."
    )


def start_claude_sso_login(*, email: str = "") -> str:
    """
    Launch Claude Code SSO login inside WSL (opens browser).
    Runs hidden — only the browser should appear on the taskbar.
    """
    claude = find_wsl_claude()
    if not claude:
        raise RuntimeError(
            "Claude Code was not found in WSL. Open Claude Code once in Cursor/VS Code "
            "on WSL, or install the Claude Code CLI."
        )
    email_flag = f' --email "{email}"' if email.strip() else ""
    # No interactive "press enter" wait — that was what kept a console open.
    bash = f'"{claude}" auth login --sso{email_flag}'
    start_hidden(["wsl", "-e", "bash", "-lc", bash])
    return (
        "Opened Claude SSO login in your browser.\n"
        "Finish SSO sign-in there, then click Refresh auth."
    )


def check_github_auth(explicit_token: str = "", use_wsl: bool = True) -> tuple[bool, str]:
    """Return (ok, detail). Verifies a usable token against the GitHub API.

    Takes the same explicit_token/use_wsl inputs as the review flow's own
    resolve_github_token() call, so this status check reflects whichever
    auth source a real run would actually use.
    """
    from app.wsl_auth import resolve_github_token

    try:
        token, source = resolve_github_token(explicit_token, use_wsl=use_wsl)
    except Exception as exc:  # noqa: BLE001
        return False, f"not signed in ({exc})"

    try:
        import requests

        resp = requests.get(
            "https://api.github.com/user",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "peer-review-app",
            },
            timeout=20,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"connection failed ({exc})"

    if resp.status_code == 401:
        return False, "credentials invalid / expired"
    if resp.status_code != 200:
        return False, f"API error HTTP {resp.status_code}"

    login = (resp.json() or {}).get("login") or "unknown"
    source_label = {
        "wsl-git-credentials": "WSL",
        "windows-gcm": "Windows GCM",
        "settings": "Settings token",
    }.get(source, source)
    return True, f"{login} via {source_label}"


def check_claude_auth() -> tuple[bool, str]:
    """Return (ok, detail) for WSL Claude Code SSO session."""
    claude = find_wsl_claude()
    if not claude:
        return False, "CLI not found in WSL"
    script = f'"{claude}" auth status 2>/dev/null'
    try:
        completed = subprocess.run(
            ["wsl", "-e", "bash", "-lc", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
            **no_window_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"status check failed ({exc})"
    raw = (completed.stdout or "").strip()
    if not raw:
        return False, "not signed in"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return False, raw[:120]
    if data.get("loggedIn"):
        email = data.get("email") or "unknown"
        org = data.get("orgName") or ""
        method = data.get("authMethod") or "claude.ai"
        suffix = f" / {org}" if org else ""
        return True, f"{email}{suffix} ({method})"
    return False, "not signed in"


def win_path_to_wsl(path: Path) -> str:
    resolved = path.resolve()
    drive = resolved.drive.rstrip(":").lower()
    rest = resolved.as_posix()
    if ":" in rest:
        rest = rest.split(":", 1)[1]
    return f"/mnt/{drive}{rest}"
