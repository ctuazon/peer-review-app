"""The structured review contract: JSON schema, parsing, validation, fingerprints.

Ported from the Laravel app (ReviewSchema, ReviewResult, Finding, Fingerprint).
The schema is the only output contract the model gets; the old FILE:/LINE:
text format survives only as a fallback parser (review_parse.py) for when the
JSON can't be read.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from app.diff_model import FileDiff
from app.review_parse import ReviewComment, parse_review_comments

SEVERITIES = ("blocker", "major", "minor", "nit")
SEVERITY_RANK = {name: i for i, name in enumerate(SEVERITIES)}
SEVERITY_LABELS = {"blocker": "Blocker", "major": "Major", "minor": "Minor", "nit": "Nit"}
VERDICTS = ("approve", "approve_with_nits", "changes_requested")
PRIOR_STATUSES = ("fixed", "open", "changed", "withdrawn")
CONFIDENCES = ("high", "medium", "low")
NOT_IN_DIFF = "line not in diff"
# Never offered to the model. A run whose output can't be read reached no
# verdict, and must not read as a clean bill of health.
INCONCLUSIVE = "inconclusive"


def _obj(properties: dict[str, Any]) -> dict[str, Any]:
    # Structured output wants every property required and the object closed.
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


_NULLABLE_STR = {"type": ["string", "null"]}
_NULLABLE_INT = {"type": ["integer", "null"]}


def review_schema() -> dict[str, Any]:
    finding = _obj(
        {
            "severity": {"type": "string", "enum": list(SEVERITIES)},
            "file": {"type": "string", "description": "Repository-relative path exactly as in the diff."},
            "line": {
                **_NULLABLE_INT,
                "description": "The number shown against this line in the annotated diff. Null only when not tied to a line.",
            },
            "start_line": {**_NULLABLE_INT, "description": "First line of a multi-line finding; null for one line."},
            "side": {
                "type": "string",
                "enum": ["RIGHT", "LEFT"],
                "description": "RIGHT for added/context lines (R in the diff), LEFT for deleted lines (L).",
            },
            "symbol": {"type": "string", "description": "Enclosing class/function, so the finding survives lines moving."},
            "anchor": {"type": "string", "description": "A short, stable code snippet from that spot."},
            "title": {"type": "string", "description": "Short title, no severity prefix."},
            "body": {"type": "string", "description": "One to three sentences: the defect, why it matters, the fix."},
            "suggestion": {
                **_NULLABLE_STR,
                "description": "Replacement source for exactly the cited lines. Null unless the fix is unambiguous and fits in them. No fences.",
            },
            "confidence": {"type": "string", "enum": list(CONFIDENCES)},
            "also_flagged_by": {**_NULLABLE_STR, "description": "Another reviewer who independently raised this."},
            "also_flagged_url": {**_NULLABLE_STR, "description": "The url of that reviewer's comment, copied exactly."},
        }
    )
    prior = _obj(
        {
            "fp": {**_NULLABLE_STR, "description": "Fingerprint of one of your earlier findings, copied exactly. Null for another reviewer's."},
            "title": {"type": "string"},
            "status": {"type": "string", "enum": list(PRIOR_STATUSES)},
            "note": {"type": "string", "description": "One line; for fixed, cite the line or change that fixed it."},
            "file": _NULLABLE_STR,
            "line": _NULLABLE_INT,
            "raised_by": {**_NULLABLE_STR, "description": "Who raised it, when it was not you."},
        }
    )
    disagreement = _obj(
        {
            "comment_id": {**_NULLABLE_INT, "description": "Id of the inline comment being refuted."},
            "reviewer": {"type": "string"},
            "url": _NULLABLE_STR,
            "claim": {"type": "string", "description": "What they claimed, one line."},
            "rebuttal": {"type": "string", "description": "What is wrong, the evidence, and what their fix would break."},
        }
    )
    credit = _obj({"reviewer": {"type": "string"}, "point": {"type": "string"}, "url": _NULLABLE_STR})
    return _obj(
        {
            "verdict": {"type": "string", "enum": list(VERDICTS)},
            "verdict_reason": {"type": "string", "description": "One sentence; name CI when it is red."},
            "scope_note": {"type": "string", "description": "One sentence: read closely vs sampled; conventions documented vs inferred."},
            "tests_note": {"type": "string", "description": "One sentence on test coverage of the change and the ticket."},
            "findings": {"type": "array", "items": finding, "description": "Most severe first. Empty when nothing is wrong."},
            "prior_findings": {"type": "array", "items": prior, "description": "Re-review only; empty otherwise."},
            "disagreements": {"type": "array", "items": disagreement},
            "credits": {"type": "array", "items": credit},
            "report": {
                "type": "string",
                "description": "Markdown for anything the focus prompt asks for that has no field here, such as a table, lead statuses or open questions. Empty when there is nothing to add.",
            },
        }
    )


def verification_schema() -> dict[str, Any]:
    return _obj(
        {
            "verdicts": {
                "type": "array",
                "items": _obj(
                    {
                        "index": {"type": "integer"},
                        "verdict": {"type": "string", "enum": ["confirmed", "rejected", "revised"]},
                        "reason": {"type": "string"},
                        "revised_title": _NULLABLE_STR,
                        "revised_body": _NULLABLE_STR,
                        "revised_severity": _NULLABLE_STR,
                    }
                ),
            }
        }
    )


@dataclass
class PriorStatus:
    title: str
    status: str
    note: str = ""
    fp: str | None = None
    file: str | None = None
    line: int | None = None
    raised_by: str | None = None
    # Filled in by the app when matched to a GitHub thread.
    thread_id: str = ""
    thread_url: str = ""
    comment_id: int | None = None
    own: bool = True

    @property
    def addressed(self) -> bool:
        return self.status in ("fixed", "withdrawn")


@dataclass
class Disagreement:
    reviewer: str
    claim: str
    rebuttal: str
    comment_id: int | None = None
    url: str | None = None
    posted_url: str = ""


@dataclass
class Credit:
    reviewer: str
    point: str
    url: str | None = None


@dataclass
class ReviewResult:
    verdict: str = "approve"
    verdict_reason: str = ""
    scope_note: str = ""
    tests_note: str = ""
    report: str = ""
    findings: list[ReviewComment] = field(default_factory=list)
    prior: list[PriorStatus] = field(default_factory=list)
    disagreements: list[Disagreement] = field(default_factory=list)
    credits: list[Credit] = field(default_factory=list)
    # Entries the model returned that couldn't be used, with a reason each.
    rejected: list[ReviewComment] = field(default_factory=list)
    # Findings the verification pass rejected; kept so the user can restore them.
    dropped_by_verifier: list[ReviewComment] = field(default_factory=list)
    # Findings dropped by the severity floor / nit cap / repeat filter.
    filtered_out: list[ReviewComment] = field(default_factory=list)
    parse_mode: str = "json"  # json | legacy
    raw_text: str = ""

    def by_severity(self, severity: str) -> list[ReviewComment]:
        return [f for f in self.findings if f.severity == severity]

    def severity_counts(self) -> dict[str, int]:
        return {s: len(self.by_severity(s)) for s in SEVERITIES}

    def headline(self) -> str:
        blockers = len(self.by_severity("blocker"))
        if self.verdict == INCONCLUSIVE:
            return "Inconclusive: no verdict reached"
        if self.verdict == "approve":
            return "Approve"
        if self.verdict == "approve_with_nits":
            return "Approve with nits"
        if blockers:
            return f"Changes requested: {blockers} blocker{'s' if blockers != 1 else ''}"
        return "Changes requested"

    def to_dict(self) -> dict[str, Any]:
        """Compact structured form stored in history (not the raw schema)."""
        return {
            "verdict": self.verdict,
            "verdict_reason": self.verdict_reason,
            "scope_note": self.scope_note,
            "tests_note": self.tests_note,
            "report": self.report,
            "findings": [finding_to_dict(f) for f in self.findings],
            "prior_findings": [
                {"fp": p.fp, "title": p.title, "status": p.status, "note": p.note,
                 "file": p.file, "line": p.line, "raised_by": p.raised_by}
                for p in self.prior
            ],
            "disagreements": [
                {"comment_id": d.comment_id, "reviewer": d.reviewer, "url": d.url,
                 "claim": d.claim, "rebuttal": d.rebuttal}
                for d in self.disagreements
            ],
            "credits": [{"reviewer": c.reviewer, "point": c.point, "url": c.url} for c in self.credits],
        }


def finding_to_dict(f: ReviewComment) -> dict[str, Any]:
    return {
        "severity": f.severity,
        "file": f.file_path,
        "line": f.line,
        "start_line": f.start_line,
        "side": f.side,
        "symbol": f.symbol,
        "anchor": f.anchor,
        "title": f.title,
        "body": f.comment,
        "suggestion": f.suggestion,
        "confidence": f.confidence,
        "also_flagged_by": f.also_flagged_by,
        "also_flagged_url": f.also_flagged_url,
        "fp": f.fp,
    }


# --- parsing ---------------------------------------------------------------

_FENCE_RE = re.compile(r"^```(?:json)?\s*\n(.*?)\n```\s*$", re.DOTALL)


def extract_json_object(text: str) -> dict[str, Any] | None:
    """The first JSON object in `text`, tolerating code fences and chatter."""
    text = (text or "").strip()
    if not text:
        return None
    fenced = _FENCE_RE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            value, _end = decoder.raw_decode(text, match.start())
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _str_or_none(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def finding_from_dict(payload: dict[str, Any]) -> ReviewComment:
    """Never raises; an unusable entry comes back with `problem` set."""
    problems: list[str] = []
    severity = str(payload.get("severity") or "").strip().lower()
    if severity not in SEVERITIES:
        problems.append(f"unknown severity '{severity}'" if severity else "no severity")
    path = str(payload.get("file") or "").strip().strip("`").removeprefix("./")
    if not path:
        problems.append("no file")
    raw_line = payload.get("line")
    line = _int_or_none(raw_line)
    if raw_line not in (None, "") and line is None:
        problems.append(f"line {raw_line!r} is not a number")
    start_line = _int_or_none(payload.get("start_line"))
    if start_line is not None and (line is None or start_line >= line):
        start_line = None
    confidence = str(payload.get("confidence") or "medium").lower()
    return ReviewComment(
        file_path=path,
        line=line,
        side="LEFT" if str(payload.get("side") or "").upper() == "LEFT" else "RIGHT",
        severity=severity if severity in SEVERITIES else "nit",
        comment=str(payload.get("body") or "").strip(),
        title=str(payload.get("title") or "").strip(),
        start_line=start_line,
        symbol=str(payload.get("symbol") or "").strip(),
        anchor=str(payload.get("anchor") or "").strip(),
        suggestion=_str_or_none(payload.get("suggestion")),
        confidence=confidence if confidence in CONFIDENCES else "medium",
        also_flagged_by=_str_or_none(payload.get("also_flagged_by")),
        also_flagged_url=_str_or_none(payload.get("also_flagged_url")),
        fp=str(payload.get("fp") or ""),
        problem="; ".join(problems),
        include=not problems,
    )


def result_from_payload(payload: dict[str, Any]) -> ReviewResult:
    findings: list[ReviewComment] = []
    rejected: list[ReviewComment] = []
    for item in payload.get("findings") or []:
        if not isinstance(item, dict):
            continue
        finding = finding_from_dict(item)
        (rejected if finding.problem else findings).append(finding)

    prior: list[PriorStatus] = []
    for item in payload.get("prior_findings") or []:
        if not isinstance(item, dict) or not str(item.get("title") or "").strip():
            continue
        status = str(item.get("status") or "open").lower()
        prior.append(
            PriorStatus(
                title=str(item["title"]).strip(),
                status=status if status in PRIOR_STATUSES else "open",
                note=str(item.get("note") or "").strip(),
                fp=_str_or_none(item.get("fp")),
                file=_str_or_none(item.get("file")),
                line=_int_or_none(item.get("line")),
                raised_by=_str_or_none(item.get("raised_by")),
                own=_str_or_none(item.get("raised_by")) is None,
            )
        )

    disagreements = [
        Disagreement(
            reviewer=str(item.get("reviewer") or "a reviewer"),
            claim=str(item.get("claim") or "").strip(),
            rebuttal=str(item.get("rebuttal") or "").strip(),
            comment_id=_int_or_none(item.get("comment_id")),
            url=_str_or_none(item.get("url")),
        )
        for item in payload.get("disagreements") or []
        if isinstance(item, dict) and str(item.get("rebuttal") or "").strip()
    ]
    credits = [
        Credit(
            reviewer=str(item.get("reviewer") or "a prior reviewer"),
            point=str(item.get("point") or "").strip(),
            url=_str_or_none(item.get("url")),
        )
        for item in payload.get("credits") or []
        if isinstance(item, dict) and str(item.get("point") or "").strip()
    ]

    verdict = str(payload.get("verdict") or "").lower()
    result = ReviewResult(
        verdict=verdict if verdict in VERDICTS else "changes_requested",
        verdict_reason=str(payload.get("verdict_reason") or "").strip(),
        scope_note=str(payload.get("scope_note") or "").strip(),
        tests_note=str(payload.get("tests_note") or "").strip(),
        report=str(payload.get("report") or "").strip(),
        findings=findings,
        prior=prior,
        disagreements=disagreements,
        credits=credits,
        rejected=rejected,
        parse_mode="json",
    )
    if verdict not in VERDICTS:
        result.verdict = derive_verdict(result.findings)
    return result


def derive_verdict(findings: list[ReviewComment]) -> str:
    severities = {f.severity for f in findings}
    if severities & {"blocker", "major"}:
        return "changes_requested"
    if severities:
        return "approve_with_nits"
    return "approve"


def inconclusive(text: str, reason: str) -> ReviewResult:
    """A run that produced nothing readable; its raw output is kept so the
    user can see what the model actually said."""
    return ReviewResult(verdict=INCONCLUSIVE, verdict_reason=reason, parse_mode="legacy", raw_text=text)


def parse_review_output(text: str, structured: dict[str, Any] | None = None) -> ReviewResult:
    """Structured output first, then any JSON object in the text, then the
    legacy FILE:/LINE: blocks. `parse_mode` says which one worked."""
    payload = structured if isinstance(structured, dict) else extract_json_object(text)
    if payload is not None and ("findings" in payload or "verdict" in payload):
        result = result_from_payload(payload)
        result.raw_text = text or json.dumps(payload)
        return result

    comments = parse_review_comments(text)
    if not comments:
        return inconclusive(text, "The model's output couldn't be read as a review, so no verdict was reached.")
    # Legacy praise entries are not findings; the verdict covers them.
    usable = [c for c in comments if not c.problem and c.severity != "praise"]
    rejected = [c for c in comments if c.problem]
    return ReviewResult(
        verdict=derive_verdict(usable),
        verdict_reason="",
        findings=usable,
        rejected=rejected,
        parse_mode="legacy",
        raw_text=text,
    )


# --- filtering and validation ---------------------------------------------

def apply_floor(result: ReviewResult, floor: str = "nit", max_nits: int | None = None) -> ReviewResult:
    """Drop findings below `floor`, cap nits, and order most severe first."""
    floor_rank = SEVERITY_RANK.get(floor, SEVERITY_RANK["nit"])
    kept = sorted(
        (f for f in result.findings if SEVERITY_RANK.get(f.severity, 99) <= floor_rank),
        key=lambda f: SEVERITY_RANK.get(f.severity, 99),
    )
    dropped = [f for f in result.findings if SEVERITY_RANK.get(f.severity, 99) > floor_rank]
    if max_nits is not None:
        nits = 0
        capped: list[ReviewComment] = []
        for f in kept:
            if f.severity == "nit":
                nits += 1
                if nits > max_nits:
                    dropped.append(f)
                    continue
            capped.append(f)
        kept = capped
    result.findings = kept
    result.filtered_out.extend(dropped)
    return result


def validate_lines(result: ReviewResult, diffs: dict[str, FileDiff]) -> list[ReviewComment]:
    """Mark findings GitHub would 422 on. Returns the ones marked.

    A file-level finding (no line) is fine: it goes in the summary. A finding
    on a file outside the diff, or a line outside a hunk, gets
    problem=NOT_IN_DIFF: it stays included, but is demoted to the summary
    instead of being sent inline (which GitHub would 422).
    """
    bad: list[ReviewComment] = []
    for f in result.findings:
        if f.line is None:
            continue
        diff = diffs.get(f.file_path)
        if diff is None or not diff.accepts(f.line, f.side):
            f.problem = NOT_IN_DIFF if diff is not None else "file not in this PR's diff"
            bad.append(f)
            continue
        if f.start_line is not None and not diff.accepts(f.start_line, f.side):
            f.start_line = None
    return bad


# --- fingerprints and markers ----------------------------------------------

def _normalise(text: str) -> str:
    text = re.sub(r"[^\w\s:\\>-]", "", (text or "").lower())
    return re.sub(r"\s+", " ", text).strip()


def fingerprint(finding: ReviewComment, diff: FileDiff | None = None) -> str:
    """Identity across passes: path + symbol + title, line left out so it
    survives code moving."""
    symbol = finding.symbol
    if not symbol and finding.line is not None and diff is not None:
        symbol = diff.enclosing_symbol(finding.line) or ""
    raw = f"{finding.file_path}|{_normalise(symbol)}|{_normalise(finding.title or finding.comment[:80])}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def assign_fingerprints(findings: list[ReviewComment], diffs: dict[str, FileDiff]) -> None:
    for f in findings:
        if not f.fp:
            f.fp = fingerprint(f, diffs.get(f.file_path))


FP_MARKER_RE = re.compile(r"<!-- pra:fp=(?P<fp>[0-9a-f]{12})(?P<attrs>[^>]*?)\s*-->")
REVIEW_MARKER_RE = re.compile(r"<!-- peer-review-app(?P<attrs>[^>]*?)\s*-->")
_ATTR_RE = re.compile(r"(\w+)=(\S+)")


def fp_marker(fp: str, severity: str, head_sha: str) -> str:
    return f"<!-- pra:fp={fp} sev={severity} sha={head_sha} -->"


def parse_fp_marker(body: str) -> dict[str, str] | None:
    m = FP_MARKER_RE.search(body or "")
    if not m:
        return None
    attrs = dict(_ATTR_RE.findall(m.group("attrs")))
    return {"fp": m.group("fp"), "sev": attrs.get("sev", ""), "sha": attrs.get("sha", "")}


def review_marker(head_sha: str, base_sha: str = "") -> str:
    base = f" base={base_sha}" if base_sha else ""
    return f"<!-- peer-review-app sha={head_sha}{base} v=1 -->"


def parse_review_marker(body: str) -> dict[str, str] | None:
    m = REVIEW_MARKER_RE.search(body or "")
    if not m:
        return None
    attrs = dict(_ATTR_RE.findall(m.group("attrs")))
    return {"sha": attrs.get("sha", ""), "base": attrs.get("base", ""), "marker": m.group(0)}
