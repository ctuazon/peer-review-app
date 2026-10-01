"""Find credentials in the lines a PR adds, and mask them before anything is
sent to the model (port of the Laravel app's SecretScanner).

Deliberately narrow: high-confidence patterns with a recognisable shape, no
entropy guessing. Unlike the Laravel app, the desktop masks the values in the
prompt it sends, so a leaked key isn't also copied to a third party.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace

from app.diff_model import DiffHunk, DiffLine, FileDiff

PATTERNS: dict[str, re.Pattern[str]] = {
    "AWS access key id": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "AWS secret access key": re.compile(
        r"\baws_secret_access_key\s*[=:]\s*['\"]?[A-Za-z0-9/+]{40}['\"]?", re.IGNORECASE
    ),
    "GitHub personal access token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    "GitHub fine-grained token": re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}\b"),
    "Slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    "Google API key": re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b"),
    "Stripe secret key": re.compile(r"\bsk_live_[0-9a-zA-Z]{20,}\b"),
    "Atlassian API token": re.compile(r"\bATATT[A-Za-z0-9_\-=]{20,}\b"),
    "Anthropic API key": re.compile(r"\bsk-ant-[A-Za-z0-9\-_]{20,}\b"),
    "OpenAI API key": re.compile(r"\bsk-[A-Za-z0-9]{32,}\b"),
    "private key block": re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"),
    "JSON Web Token": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    "basic auth in a URL": re.compile(
        r"\b[a-z][a-z0-9+.-]*://[^/\s:@]+:(?P<secret>[^/\s:@]+)@(?P<host>[A-Za-z0-9.-]+)", re.IGNORECASE
    ),
    # No \b before the name: `$test_password` has an underscore (a word char).
    "hardcoded password assignment": re.compile(
        r"(?<![a-z0-9])(?:password|passwd|secret|api_?key|token)(?:[_-]?(?:key|token|secret))?['\"]?\]?"
        r"\s*(?:=>|[=:])\s*['\"](?P<secret>[^'\"\s${|]{8,})['\"]",
        re.IGNORECASE,
    ),
}

# Judged against the matched value alone, never the whole line.
_BENIGN = re.compile(
    r"(?:(?<![a-z])(?:example|sample|dummy|placeholder|redacted|changeme|your[-_]?|fake|test(?:ing)?)(?![a-z])"
    r"|x{4,}|<[^>]+>|\$\{|%s)",
    re.IGNORECASE,
)
_FRAMEWORK_VALUE = re.compile(
    r"^(?:required|nullable|sometimes|confirmed|encrypted|hashed|filled|prohibited|accepted|declined|boolean"
    r"|integer|numeric|string|array|object|collection|datetime|immutable_datetime|timestamp|date|json"
    r"|current_password|bail)(?:[:_].*)?$",
    re.IGNORECASE,
)
_PLACEHOLDER_HOST = re.compile(r"(?:^|\.)(?:example\.(?:com|org|net)|test|invalid|localhost)$", re.IGNORECASE)
_KEY_END = re.compile(r"-----END [A-Z ]*PRIVATE KEY-----")


@dataclass
class SecretFinding:
    path: str
    line: int | None
    kind: str
    excerpt: str  # the line with every value replaced by [redacted]


def _spans(text: str) -> tuple[str | None, list[tuple[int, int]]]:
    if not text.strip():
        return None, []
    kind: str | None = None
    spans: list[tuple[int, int]] = []
    for label, pattern in PATTERNS.items():
        for m in pattern.finditer(text):
            group = "secret" if "secret" in pattern.groupindex and m.group("secret") else 0
            value, start = m.group(group), m.start(group)
            if _BENIGN.search(value) or _FRAMEWORK_VALUE.match(value):
                continue
            if "host" in pattern.groupindex and m.group("host") and _PLACEHOLDER_HOST.search(m.group("host")):
                continue
            kind = kind or label
            spans.append((start, len(value)))
    return kind, spans


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[list[int]] = []
    for start, length in sorted(spans):
        if merged and start <= merged[-1][0] + merged[-1][1]:
            end = max(merged[-1][0] + merged[-1][1], start + length)
            merged[-1][1] = end - merged[-1][0]
        else:
            merged.append([start, length])
    return [(s, n) for s, n in merged]


def _obscure(value: str) -> str:
    # Enough left to tell two keys apart, not enough to use either.
    if len(value) < 16:
        return "*" * len(value)
    return value[:4] + "*" * (len(value) - 8) + value[-4:]


def _replace_spans(text: str, spans: list[tuple[int, int]], fn) -> str:
    for start, length in reversed(_merge(spans)):
        text = text[:start] + fn(text[start : start + length]) + text[start + length :]
    return text


def scan_line(path: str, line: int | None, text: str) -> SecretFinding | None:
    kind, spans = _spans(text)
    if kind is None:
        return None
    excerpt = _replace_spans(text, spans, lambda _v: "[redacted]").strip()
    if len(excerpt) > 140:
        excerpt = excerpt[:139] + "…"
    return SecretFinding(path, line, kind, excerpt)


def scan_diffs(diffs: dict[str, FileDiff]) -> list[SecretFinding]:
    found: list[SecretFinding] = []
    for diff in diffs.values():
        for hunk in diff.hunks:
            for ln in hunk.lines:
                if ln.origin == "+":
                    hit = scan_line(diff.path, ln.new_line, ln.text)
                    if hit:
                        found.append(hit)
    return found


class _Masker:
    """Masks values line by line; a private key masks every line to its footer."""

    def __init__(self) -> None:
        self.in_key = False

    def line(self, text: str) -> str:
        if self.in_key:
            self.in_key = not _KEY_END.search(text)
            return re.sub(r"\S", "*", text) if self.in_key else text
        _kind, spans = _spans(text)
        if PATTERNS["private key block"].search(text):
            self.in_key = True
        return _replace_spans(text, spans, _obscure)


def mask_text(text: str) -> str:
    masker = _Masker()
    return "\n".join(masker.line(ln) for ln in (text or "").split("\n"))


def mask_diffs(diffs: dict[str, FileDiff]) -> dict[str, FileDiff]:
    """Same diffs with matched values masked; line numbers untouched."""
    out: dict[str, FileDiff] = {}
    for path, diff in diffs.items():
        masker = _Masker()
        hunks = [
            DiffHunk(
                h.old_start,
                h.new_start,
                h.header,
                [DiffLine(ln.origin, masker.line(ln.text), ln.old_line, ln.new_line) for ln in h.lines],
            )
            for h in diff.hunks
        ]
        out[path] = replace(diff, hunks=hunks)
    return out


def render_findings(findings: list[SecretFinding]) -> str:
    if not findings:
        return (
            "The credential scan of added lines found nothing. The scan is narrow by design; "
            "your own reading of the diff still governs."
        )
    rows = "\n".join(
        f"- {f.path}:{f.line if f.line is not None else '?'}: {f.kind}: `{f.excerpt}`" for f in findings
    )
    return (
        "Possible credentials in added lines (values masked before you saw them). A match is evidence, "
        "not a verdict: confirm each against the diff.\n\n" + rows
    )
