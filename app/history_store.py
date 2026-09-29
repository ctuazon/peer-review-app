"""Persist and recall past peer reviews, explanations, and Ask Claude runs."""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from app import HISTORY_PATH, ensure_data_dir, load_json, save_json

MAX_HISTORY_ENTRIES = 200

MODE_LABELS = {
    "review": "Peer review",
    "explain": "Explain PR",
    "ask": "Ask Claude",
}


@dataclass
class HistoryEntry:
    id: str
    created_at: str
    mode: str
    pr_url: str
    pr_title: str = ""
    pr_ref: str = ""
    story: str = ""
    reviewer_prompt_id: str = ""
    reviewer_prompt_name: str = ""
    reviewer_prompt_content: str = ""
    result: str = ""
    summary_lines: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HistoryEntry":
        summary = data.get("summary_lines") or []
        if not isinstance(summary, list):
            summary = [str(summary)]
        return cls(
            id=str(data.get("id") or uuid.uuid4()),
            created_at=str(data.get("created_at") or ""),
            mode=str(data.get("mode") or "ask"),
            pr_url=str(data.get("pr_url") or ""),
            pr_title=str(data.get("pr_title") or ""),
            pr_ref=str(data.get("pr_ref") or ""),
            story=str(data.get("story") or ""),
            reviewer_prompt_id=str(data.get("reviewer_prompt_id") or ""),
            reviewer_prompt_name=str(data.get("reviewer_prompt_name") or ""),
            reviewer_prompt_content=str(data.get("reviewer_prompt_content") or ""),
            result=str(data.get("result") or ""),
            summary_lines=[str(s) for s in summary],
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def mode_label(self) -> str:
        return MODE_LABELS.get(self.mode, self.mode.title() or "Run")

    def list_title(self) -> str:
        when = self.created_at.replace("T", " ")[:16] if self.created_at else "unknown"
        target = self.pr_ref or self.pr_title or self.pr_url or "(no PR)"
        if len(target) > 48:
            target = target[:45] + "…"
        return f"{when}  ·  {self.mode_label}  ·  {target}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _load_raw() -> dict[str, Any]:
    ensure_data_dir()
    data = load_json(HISTORY_PATH, {"entries": []})
    if not isinstance(data, dict):
        return {"entries": []}
    if "entries" not in data or not isinstance(data["entries"], list):
        data["entries"] = []
    return data


def list_history() -> list[HistoryEntry]:
    entries = [HistoryEntry.from_dict(item) for item in _load_raw()["entries"]]
    # Newest first.
    entries.sort(key=lambda e: e.created_at or "", reverse=True)
    return entries


def pr_key(pr_url: str) -> str:
    """Stable identity for a PR: 'owner/repo#number', lowercased. '' if unparseable."""
    from app.github_pr import parse_pr_url

    try:
        ref = parse_pr_url(pr_url or "")
    except ValueError:
        return ""
    return f"{ref.full_name.lower()}#{ref.number}"


def list_reviews_for_pr(pr_url: str) -> list[HistoryEntry]:
    """Past peer-review runs for the same repo + PR number, newest first."""
    key = pr_key(pr_url)
    if not key:
        return []
    return [
        e
        for e in list_history()
        if e.mode == "review" and e.result and pr_key(e.pr_url) == key
    ]


def get_history_entry(entry_id: str) -> HistoryEntry | None:
    for entry in list_history():
        if entry.id == entry_id:
            return entry
    return None


def save_all(entries: list[HistoryEntry]) -> None:
    # Persist newest first, capped.
    ordered = sorted(entries, key=lambda e: e.created_at or "", reverse=True)
    save_json(
        HISTORY_PATH,
        {"entries": [e.to_dict() for e in ordered[:MAX_HISTORY_ENTRIES]]},
    )


def add_history_entry(
    *,
    mode: str,
    pr_url: str,
    result: str,
    story: str = "",
    pr_title: str = "",
    pr_ref: str = "",
    reviewer_prompt_id: str = "",
    reviewer_prompt_name: str = "",
    reviewer_prompt_content: str = "",
    summary_lines: list[str] | None = None,
) -> HistoryEntry:
    entries = list_history()
    entry = HistoryEntry(
        id=str(uuid.uuid4()),
        created_at=_now_iso(),
        mode=mode,
        pr_url=pr_url.strip(),
        pr_title=(pr_title or "").strip(),
        pr_ref=(pr_ref or "").strip(),
        story=story or "",
        reviewer_prompt_id=reviewer_prompt_id or "",
        reviewer_prompt_name=reviewer_prompt_name or "",
        reviewer_prompt_content=reviewer_prompt_content or "",
        result=(result or "").strip(),
        summary_lines=list(summary_lines or []),
    )
    entries.insert(0, entry)
    save_all(entries)
    return entry


def delete_history_entry(entry_id: str) -> None:
    entries = [e for e in list_history() if e.id != entry_id]
    save_all(entries)


def clear_history() -> None:
    save_all([])
