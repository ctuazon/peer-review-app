"""Persist last-known GitHub / Claude auth light status across app restarts."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app import DATA_DIR, ensure_data_dir, load_json, save_json

AUTH_STATUS_PATH = DATA_DIR / "auth_status.json"

DEFAULT_AUTH_STATUS: dict[str, Any] = {
    "github": {"ok": None, "detail": "", "checked_at": ""},
    "claude": {"ok": None, "detail": "", "checked_at": ""},
}


def load_auth_status() -> dict[str, Any]:
    ensure_data_dir()
    data = load_json(AUTH_STATUS_PATH, {})
    merged = {
        "github": {**DEFAULT_AUTH_STATUS["github"], **(data.get("github") or {})},
        "claude": {**DEFAULT_AUTH_STATUS["claude"], **(data.get("claude") or {})},
    }
    return merged


def save_auth_status(
    *,
    github_ok: bool,
    github_detail: str,
    claude_ok: bool,
    claude_detail: str,
) -> None:
    ensure_data_dir()
    now = datetime.now(timezone.utc).isoformat()
    save_json(
        AUTH_STATUS_PATH,
        {
            "github": {
                "ok": bool(github_ok),
                "detail": github_detail,
                "checked_at": now,
            },
            "claude": {
                "ok": bool(claude_ok),
                "detail": claude_detail,
                "checked_at": now,
            },
        },
    )


def clear_auth_status() -> None:
    """Reset persisted lights to signed-out."""
    save_auth_status(
        github_ok=False,
        github_detail="cleared",
        claude_ok=False,
        claude_detail="cleared",
    )
