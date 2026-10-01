"""Shared paths and config helpers."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CONFIG_PATH = DATA_DIR / "config.json"
PROMPTS_PATH = DATA_DIR / "prompts.json"
HISTORY_PATH = DATA_DIR / "history.json"

DEFAULT_CONFIG = {
    "github_token": "",
    "use_wsl_github_auth": True,
    "use_wsl_claude": True,
    "claude_mode": "cli",  # "cli" | "api" | "wsl"
    "anthropic_api_key": "",
    "claude_cli_path": "claude",
    "claude_model": "claude-opus-5-5",
    # Cheaper/faster model for the bot-comment triage and self-review verdict
    # stages (classification-style yes/no judgments), leaving claude_model
    # for the actual fix-drafting stage. Blank disables the override.
    "bot_cheap_model": "claude-haiku-4-5-20251001",
    "claude_sso_email": "czyrus.tuazon@traderinteractive.com",
    # Jira ticket context. None/blank values fall back to the JIRA_* env vars
    # (or a .env file next to main.py); see app/jira.py.
    "jira_enabled": None,
    "jira_base_url": "",
    "jira_email": "",
    "jira_api_token": "",
    # Peer review defaults; .github/pr-review.yml and the review tab override.
    "review_effort": "medium",
    "review_mode": "agentic",  # agentic (local checkout + Read/Grep/Glob) | single-shot
    "verify_findings": False,
    "claude_timeout_minutes": 15,
    # Warn before a run estimated above this many dollars (0 disables).
    "warn_review_usd": 2.0,
    "checkout_keep_days": 7,
}

# Defaults that were never a user choice, upgraded on load.
_RETIRED_DEFAULTS = {"claude_model": {"claude-sonnet-4-20250514"}}
# Stored in the OS credential vault (Windows Credential Manager) when keyring
# is available; data/config.json then keeps only an empty placeholder.
SECRET_KEYS = ("github_token", "anthropic_api_key", "jira_api_token")
_KEYRING_SERVICE = "peer-review-app"


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG.copy())
    if not PROMPTS_PATH.exists():
        PROMPTS_PATH.write_text(
            json.dumps({"prompts": []}, indent=2),
            encoding="utf-8",
        )
    if not HISTORY_PATH.exists():
        HISTORY_PATH.write_text(
            json.dumps({"entries": []}, indent=2),
            encoding="utf-8",
        )


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _keyring():
    try:
        import keyring

        keyring.get_keyring()
        return keyring
    except Exception:  # noqa: BLE001 -- missing package or no usable backend
        return None


def _read_secret(name: str) -> str:
    kr = _keyring()
    if kr is None:
        return ""
    try:
        return kr.get_password(_KEYRING_SERVICE, name) or ""
    except Exception:  # noqa: BLE001
        return ""


def _write_secret(name: str, value: str) -> bool:
    kr = _keyring()
    if kr is None:
        return False
    try:
        if value:
            kr.set_password(_KEYRING_SERVICE, name, value)
        else:
            try:
                kr.delete_password(_KEYRING_SERVICE, name)
            except Exception:  # noqa: BLE001 -- nothing stored
                pass
        return True
    except Exception:  # noqa: BLE001
        return False


def secrets_backend() -> str:
    return "Windows Credential Manager" if _keyring() is not None else "data/config.json (plaintext)"


def load_config() -> dict[str, Any]:
    ensure_data_dir()
    stored = load_json(CONFIG_PATH, {})
    cfg = DEFAULT_CONFIG.copy()
    cfg.update(stored)
    for key, retired in _RETIRED_DEFAULTS.items():
        if cfg.get(key) in retired:
            cfg[key] = DEFAULT_CONFIG[key]
    plaintext = [k for k in SECRET_KEYS if stored.get(k)]
    for key in SECRET_KEYS:
        if not cfg.get(key):
            cfg[key] = _read_secret(key)
    if plaintext:
        # Move secrets saved before keyring support out of the JSON file.
        save_config(cfg)
    return cfg


def save_config(config: dict[str, Any]) -> None:
    ensure_data_dir()
    merged = DEFAULT_CONFIG.copy()
    merged.update(config)
    for key in SECRET_KEYS:
        if _write_secret(key, str(merged.get(key) or "")):
            merged[key] = ""
    save_json(CONFIG_PATH, merged)
