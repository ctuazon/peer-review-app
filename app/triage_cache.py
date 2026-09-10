"""Cache triage verdicts for bot/reviewer comments so re-running the check
doesn't re-spend a triage call on a comment that hasn't changed since the
last check."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from app import DATA_DIR, ensure_data_dir, load_json, save_json

TRIAGE_CACHE_PATH = DATA_DIR / "triage_cache.json"
MAX_CACHE_ENTRIES = 1000

TriageVerdict = tuple[bool, str, str, bool, list[str], list[str]]


def _body_hash(body: str) -> str:
    return hashlib.sha256((body or "").encode("utf-8")).hexdigest()[:16]


def _cache_key(comment_id: int, body: str) -> str:
    # Keyed on the comment's id plus a hash of its current body, so an edited
    # comment (e.g. CodeRabbit updating its own message, or a reviewer
    # editing theirs) is re-triaged instead of silently reusing a stale verdict.
    return f"{comment_id}:{_body_hash(body)}"


def _load() -> dict[str, Any]:
    ensure_data_dir()
    data = load_json(TRIAGE_CACHE_PATH, {"entries": {}})
    if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
        return {"entries": {}}
    return data


def get_cached_triage(comment_id: int, body: str) -> TriageVerdict | None:
    entry = _load()["entries"].get(_cache_key(comment_id, body))
    if not entry:
        return None
    return (
        bool(entry.get("valid")),
        str(entry.get("reason") or ""),
        str(entry.get("reply_text") or ""),
        bool(entry.get("addressed")),
        [str(p) for p in entry.get("needs_context") or []],
        [str(p) for p in entry.get("target_files") or []],
    )


def store_triage(
    comment_id: int,
    body: str,
    valid: bool,
    reason: str,
    reply_text: str,
    addressed: bool,
    needs_context: list[str],
    target_files: list[str],
) -> None:
    data = _load()
    entries = data["entries"]
    entries[_cache_key(comment_id, body)] = {
        "valid": valid,
        "reason": reason,
        "reply_text": reply_text,
        "addressed": addressed,
        "needs_context": needs_context,
        "target_files": target_files,
        "cached_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if len(entries) > MAX_CACHE_ENTRIES:
        oldest_first = sorted(entries.items(), key=lambda kv: kv[1].get("cached_at", ""))
        for key, _ in oldest_first[: len(entries) - MAX_CACHE_ENTRIES]:
            del entries[key]
    save_json(TRIAGE_CACHE_PATH, data)
