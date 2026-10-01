"""Doctor: one button that checks everything a review depends on (port of
`review:doctor --repo`). Each check returns (ok, label, detail); a failed
check never stops the others."""
from __future__ import annotations

import re
import subprocess
from typing import Any, Callable

import requests

from app import secrets_backend
from app.cost import model_info
from app.process_util import no_window_kwargs

Check = tuple[bool | None, str, str]  # None = skipped / not applicable

# Headings that mean a repo prompt still carries its own output format, which
# competes with the JSON schema (W1).
OUTPUT_FORMAT_RE = re.compile(
    r"(?im)^\s*(?:#+\s*|\*\*)?(?:output requirements|output format|response format|executive summary"
    r"|refactored code)\b|^\s*FILE:\s*<|SEVERITY:\s*blocker\|"
)


def prompt_format_problems(prompts: list[Any]) -> list[str]:
    return [p.name for p in prompts if OUTPUT_FORMAT_RE.search(p.content or "")]


def _wsl(script: str, timeout: int = 40) -> tuple[int, str]:
    try:
        done = subprocess.run(
            ["wsl", "-e", "bash", "-lc", script], capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, check=False, **no_window_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return done.returncode, (done.stdout + done.stderr).strip()


def run_doctor(config: dict[str, Any], pr_url: str = "", on_check: Callable[[Check], None] | None = None) -> list[Check]:
    from app.auth_flows import check_claude_auth, check_github_auth, find_wsl_claude
    from app.claude_runner import resolve_claude_cli
    from app.github_pr import fetch_dependabot_alerts, get_file_text, parse_pr_url
    from app.jira import JiraClient, resolve_jira_settings
    from app.prompts_store import list_prompts
    from app.repo_config import CONFIG_PATH, parse_repo_config
    from app.wsl_auth import resolve_github_token

    checks: list[Check] = []

    def add(ok: bool | None, label: str, detail: str = "") -> None:
        checks.append((ok, label, detail))
        if on_check:
            on_check((ok, label, detail))

    from app.claude_runner import api_fallback_note, effective_mode

    mode = effective_mode(config)
    note = api_fallback_note(config)
    add(None if note else True, "Claude path", note or ("Anthropic API" if mode == "api" else "Claude Code CLI"))

    wsl_claude = find_wsl_claude()
    add(bool(wsl_claude), "Claude Code in WSL", wsl_claude or "not found")
    cli = resolve_claude_cli(config.get("claude_cli_path") or "claude")
    add(bool(cli) if not wsl_claude else (True if cli else None), "Claude Code on Windows", cli or "not found (WSL is used instead)")
    ok, detail = check_claude_auth()
    add(ok, "Claude signed in", detail)

    if wsl_claude:
        code, out = _wsl("command -v gh >/dev/null && gh auth status 2>&1 | head -3 || echo 'gh not installed'")
        gh_ok = code == 0 and "Logged in" in out
        add(gh_ok, "gh logged in where Claude runs (for the gh api tools)", out.splitlines()[0] if out else "")

    model = config.get("claude_model") or ""
    info = model_info(model)
    add(info is not None, "Configured review model", f"{model} ({info.label})" if info else f"{model!r} is not a known, priced model")
    triage = config.get("bot_cheap_model") or ""
    if triage:
        add(model_info(triage) is not None, "Triage model", triage)

    token, source = "", ""
    try:
        token, source = resolve_github_token(
            explicit_token=config.get("github_token") or "", use_wsl=bool(config.get("use_wsl_github_auth", True))
        )
    except Exception as exc:  # noqa: BLE001
        add(False, "GitHub token", str(exc))
    if token:
        ok, detail = check_github_auth(config.get("github_token") or "", use_wsl=bool(config.get("use_wsl_github_auth", True)))
        add(ok, "GitHub signed in", f"{detail} via {source}")

    ref = None
    if pr_url:
        try:
            ref = parse_pr_url(pr_url)
        except ValueError as exc:
            add(False, "PR URL", str(exc))
    if ref and token:
        resp = requests.get(
            f"https://api.github.com/repos/{ref.owner}/{ref.repo}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}, timeout=30,
        )
        add(resp.ok, f"Token can read {ref.full_name}", f"HTTP {resp.status_code}")
        alerts = fetch_dependabot_alerts(ref, token=token)
        add(alerts is not None if resp.ok else None, "Dependabot alerts readable",
            f"{len(alerts)} open" if alerts is not None else "no access: the security scan will say unavailable")
        if resp.ok:
            default_branch = (resp.json() or {}).get("default_branch") or "main"
            text = get_file_text(ref, CONFIG_PATH, default_branch, token=token)
            if text is None:
                add(None, f"{CONFIG_PATH} on {default_branch}", "not present (desktop defaults apply)")
            else:
                cfg = parse_repo_config(text)
                add(not cfg.problems, f"{CONFIG_PATH} on {default_branch}",
                    "parses cleanly" if not cfg.problems else "; ".join(cfg.problems))
    elif not pr_url:
        add(None, "Repo checks", "paste a PR URL to check repo access, Dependabot and pr-review.yml")

    jira = resolve_jira_settings(config)
    if not jira.enabled:
        add(None, "Jira", "disabled")
    elif not jira.configured:
        add(None, "Jira", "not configured")
    else:
        resp = JiraClient(jira).get("/rest/api/3/myself")
        add(bool(resp is not None and resp.ok), "Jira credentials",
            "connected" if resp is not None and resp.ok else f"HTTP {getattr(resp, 'status_code', 'unreachable')}")

    try:
        prompts = list_prompts()
        bad = prompt_format_problems(prompts)
        add(not bad, f"Prompts load ({len(prompts)})",
            "no output-format sections" if not bad else "still carry their own output format: " + ", ".join(bad))
    except Exception as exc:  # noqa: BLE001
        add(False, "Prompts load", str(exc))

    try:
        import pygit2  # noqa: F401

        add(True, "pygit2 (local checkout for agentic reviews)", "installed")
    except ImportError:
        add(False, "pygit2 (local checkout for agentic reviews)", "pip install pygit2")
    backend = secrets_backend()
    add("Credential Manager" in backend, "Secret storage", backend)
    return checks


def format_report(checks: list[Check]) -> str:
    icon = {True: "✅", False: "❌", None: "➖"}
    return "\n".join(f"{icon[ok]} {label}" + (f": {detail}" if detail else "") for ok, label, detail in checks)
