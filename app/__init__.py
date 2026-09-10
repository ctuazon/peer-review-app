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
    "claude_model": "claude-sonnet-4-20250514",
    # Cheaper/faster model for the bot-comment triage and self-review verdict
    # stages (classification-style yes/no judgments), leaving claude_model
    # for the actual fix-drafting stage. Blank disables the override.
    "bot_cheap_model": "claude-haiku-4-5-20251001",
    "claude_sso_email": "czyrus.tuazon@traderinteractive.com",
}


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


def load_config() -> dict[str, Any]:
    ensure_data_dir()
    cfg = DEFAULT_CONFIG.copy()
    cfg.update(load_json(CONFIG_PATH, {}))
    return cfg


def save_config(config: dict[str, Any]) -> None:
    ensure_data_dir()
    merged = DEFAULT_CONFIG.copy()
    merged.update(config)
    save_json(CONFIG_PATH, merged)
