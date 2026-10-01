"""Fetch Jira tickets referenced by a pull request for review context.

Ported from the Laravel app (pr-review-app/app/Jira). Settings come from
data/config.json first, then the JIRA_* environment variables (or a .env file
next to main.py), so the same keys work in both apps:

    JIRA_ENABLED=true
    JIRA_BASE_URL=https://yourorg.atlassian.net
    JIRA_EMAIL=you@example.com
    JIRA_API_TOKEN=...
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

import requests

from app import ROOT

TICKET_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
MAX_TICKETS = 5
AC_HEADINGS = {
    "acceptance criteria",
    "acceptance",
    "definition of done",
    "dod",
    "requirements",
    "ac",
}
_AC_FIELDS_CACHE: dict[str, list[str]] = {}


@dataclass
class JiraSettings:
    enabled: bool
    base_url: str
    email: str
    api_token: str

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.email and self.api_token)

@dataclass
class JiraTicket:
    key: str
    summary: str
    description: str
    acceptance_criteria: str | None
    issue_type: str
    status: str
    priority: str | None = None
    labels: list[str] = field(default_factory=list)

    def to_prompt_section(self) -> str:
        lines = [
            f"### {self.key}: {self.summary}",
            f"Type: {self.issue_type}    Status: {self.status}",
        ]
        if self.labels:
            lines.append("Labels: " + ", ".join(self.labels))
        lines += [
            "",
            "**Description**",
            self.description.strip() or "_(none on the ticket)_",
            "",
            "**Acceptance criteria**",
            (self.acceptance_criteria or "").strip() or "_(none on the ticket)_",
        ]
        return "\n".join(lines)


def _read_dotenv() -> dict[str, str]:
    path = ROOT / ".env"
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name] = value
    return values


def _parse_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def resolve_jira_settings(config: dict[str, Any]) -> JiraSettings:
    """Config values win; blank ones fall back to os.environ, then .env."""
    dotenv = _read_dotenv()

    def pick(config_key: str, env_key: str) -> str:
        value = str(config.get(config_key) or "").strip()
        if value:
            return value
        return (os.environ.get(env_key) or dotenv.get(env_key) or "").strip()

    enabled_raw = config.get("jira_enabled")
    if enabled_raw is None or enabled_raw == "":
        enabled_raw = os.environ.get("JIRA_ENABLED", dotenv.get("JIRA_ENABLED", ""))
    return JiraSettings(
        enabled=_parse_bool(enabled_raw, default=True),
        base_url=pick("jira_base_url", "JIRA_BASE_URL").rstrip("/"),
        email=pick("jira_email", "JIRA_EMAIL"),
        api_token=pick("jira_api_token", "JIRA_API_TOKEN"),
    )


def extract_ticket_keys(*texts: str) -> list[str]:
    keys: list[str] = []
    for text in texts:
        for key in TICKET_KEY_RE.findall(text or ""):
            if key not in keys:
                keys.append(key)
    return keys


def _flatten_adf(node: Any) -> str:
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_flatten_adf(child) for child in node)
    if not isinstance(node, dict):
        return ""
    kind = node.get("type")
    attrs = node.get("attrs") or {}
    children = _flatten_adf(node.get("content") or [])
    if kind == "text":
        return str(node.get("text") or "")
    if kind == "hardBreak":
        return "\n"
    if kind == "paragraph":
        return "\n" if not children.strip() else children + "\n\n"
    if kind == "heading":
        return "#" * int(attrs.get("level") or 2) + " " + children.strip() + "\n\n"
    if kind in {"bulletList", "orderedList", "taskList", "table"}:
        return children + "\n"
    if kind == "listItem":
        return "- " + children.strip() + "\n"
    if kind == "taskItem":
        mark = "x" if attrs.get("state") == "DONE" else " "
        return f"- [{mark}] " + children.strip() + "\n"
    if kind == "codeBlock":
        return "```\n" + children.strip() + "\n```\n\n"
    if kind == "blockquote":
        return "> " + children.strip() + "\n\n"
    if kind == "rule":
        return "---\n\n"
    if kind == "tableRow":
        return children.strip() + "\n"
    if kind in {"tableCell", "tableHeader"}:
        return children.strip() + " | "
    if kind == "inlineCard":
        return str(attrs.get("url") or "")
    if kind == "mention":
        return "@" + str(attrs.get("text") or "")
    if kind == "emoji":
        return str(attrs.get("text") or "")
    return children


def adf_to_text(node: Any) -> str:
    return re.sub(r"\n{3,}", "\n\n", _flatten_adf(node)).strip()


def extract_acceptance_section(description: str) -> str | None:
    """Return the text under an acceptance-criteria heading, up to the next same-or-higher heading."""
    collected: list[str] = []
    capturing = False
    captured_level = 0
    for line in description.splitlines():
        heading = re.match(r"^(#{1,6})\s*(.+?)\s*:?\s*$", line)
        if heading:
            level = len(heading.group(1))
            if capturing and level <= captured_level:
                break
            if not capturing and heading.group(2).strip().lower() in AC_HEADINGS:
                capturing, captured_level = True, level
                continue
        if not capturing and re.match(
            r"^\*{0,2}(acceptance criteria|definition of done|acceptance)\*{0,2}\s*:?\s*$",
            line.strip(),
            re.IGNORECASE,
        ):
            capturing, captured_level = True, 6
            continue
        if capturing:
            collected.append(line)
    section = "\n".join(collected).strip()
    return section or None


class JiraClient:
    def __init__(self, settings: JiraSettings) -> None:
        self.settings = settings
        self.session = requests.Session()
        self.session.auth = (settings.email, settings.api_token)
        self.session.headers["Accept"] = "application/json"

    def get(self, path: str, params: dict[str, str] | None = None) -> requests.Response | None:
        url = self.settings.base_url + path
        for _ in range(3):
            try:
                response = self.session.get(url, params=params, timeout=20)
            except requests.RequestException:
                continue
            if response.status_code < 500:
                return response
        return None

    def _acceptance_fields(self) -> list[str]:
        cached = _AC_FIELDS_CACHE.get(self.settings.base_url)
        if cached is not None:
            return cached
        response = self.get("/rest/api/3/field")
        if response is None or not response.ok:
            return []
        fields = [
            str(item["id"])
            for item in response.json() or []
            if re.search(r"acceptance", str(item.get("name") or ""), re.IGNORECASE)
        ]
        _AC_FIELDS_CACHE[self.settings.base_url] = fields
        return fields

    def fetch(self, key: str) -> JiraTicket | None:
        ac_fields = self._acceptance_fields()
        response = self.get(
            f"/rest/api/3/issue/{key}",
            params={
                "fields": ",".join(
                    ["summary", "description", "issuetype", "status", "priority", "labels", *ac_fields]
                )
            },
        )
        if response is None or not response.ok:
            return None
        payload = response.json() or {}
        fields = payload.get("fields") or {}
        description = adf_to_text(fields.get("description"))
        acceptance = None
        for field_id in ac_fields:
            value = adf_to_text(fields.get(field_id))
            if value:
                acceptance = value
                break
        if acceptance is None:
            acceptance = extract_acceptance_section(description)
        return JiraTicket(
            key=str(payload.get("key") or key),
            summary=str(fields.get("summary") or ""),
            description=description,
            acceptance_criteria=acceptance,
            issue_type=str((fields.get("issuetype") or {}).get("name") or "Unknown"),
            status=str((fields.get("status") or {}).get("name") or "Unknown"),
            priority=(fields.get("priority") or {}).get("name"),
            labels=[str(label) for label in fields.get("labels") or []],
        )

    def fetch_many(self, keys: list[str]) -> list[JiraTicket]:
        return [ticket for ticket in (self.fetch(key) for key in keys[:MAX_TICKETS]) if ticket]


def fetch_pr_tickets(
    config: dict[str, Any], *, title: str, body: str, head_branch: str
) -> tuple[list[JiraTicket], list[str], str]:
    """Return (tickets, referenced keys, status note). Never raises on Jira errors."""
    settings = resolve_jira_settings(config)
    if not settings.enabled:
        return [], [], "Jira disabled"
    if not settings.configured:
        return [], [], "Jira not configured (set JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN)"
    keys = extract_ticket_keys(title, body, head_branch)
    if not keys:
        return [], [], "no Jira ticket key found in PR title, body or branch"
    try:
        tickets = JiraClient(settings).fetch_many(keys)
    except (requests.RequestException, ValueError) as exc:
        return [], keys, f"Jira fetch failed: {exc}"
    found = {ticket.key for ticket in tickets}
    missing = [key for key in keys[:MAX_TICKETS] if key not in found]
    note = "loaded Jira " + (", ".join(sorted(found)) if found else "nothing")
    if missing:
        note += f" (not found: {', '.join(missing)})"
    return tickets, keys, note


def format_tickets_for_prompt(tickets: list[JiraTicket]) -> str:
    if not tickets:
        return "No Jira ticket was loaded for this pull request. Judge it against its own stated intent."
    return "\n\n".join(ticket.to_prompt_section() for ticket in tickets)
