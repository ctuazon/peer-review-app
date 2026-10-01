import json

from app.diff_model import parse_file_patch
from app.review_parse import parse_review_comments
from app.review_schema import (
    NOT_IN_DIFF,
    apply_floor,
    extract_json_object,
    fingerprint,
    fp_marker,
    parse_fp_marker,
    parse_review_marker,
    parse_review_output,
    review_marker,
    review_schema,
    validate_lines,
)

PATCH = "@@ -1,2 +1,3 @@ def handler\n a\n+b\n c"


def _finding(**kw):
    base = {
        "severity": "major", "file": "x.py", "line": 2, "start_line": None, "side": "RIGHT",
        "symbol": "", "anchor": "b", "title": "Null deref", "body": "It crashes.",
        "suggestion": None, "confidence": "high", "also_flagged_by": None, "also_flagged_url": None,
    }
    base.update(kw)
    return base


def _payload(findings, **kw):
    out = {"verdict": "changes_requested", "verdict_reason": "r", "scope_note": "s", "tests_note": "t",
           "findings": findings, "prior_findings": [], "disagreements": [], "credits": []}
    out.update(kw)
    return out


def test_schema_closes_every_object():
    schema = review_schema()

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(schema)


def test_parses_structured_output():
    result = parse_review_output("", structured=_payload([_finding()]))
    assert result.parse_mode == "json"
    assert len(result.findings) == 1
    assert result.findings[0].title == "Null deref"


def test_parses_fenced_json_with_chatter():
    text = "Here you go:\n```json\n" + json.dumps(_payload([_finding()])) + "\n```"
    assert extract_json_object(text)["verdict"] == "changes_requested"
    result = parse_review_output("Sure. " + json.dumps(_payload([_finding()])) + " done")
    assert result.parse_mode == "json"
    assert len(result.findings) == 1
    fenced = parse_review_output("```json\n" + json.dumps(_payload([])) + "\n```")
    assert fenced.parse_mode == "json"


def test_unknown_severity_is_rejected_not_downgraded():
    result = parse_review_output(json.dumps(_payload([_finding(severity="critical")])))
    assert result.findings == []
    assert len(result.rejected) == 1
    assert "unknown severity" in result.rejected[0].problem


def test_bad_line_is_rejected():
    result = parse_review_output(json.dumps(_payload([_finding(line="twelve")])))
    assert result.rejected and "not a number" in result.rejected[0].problem


def test_clean_review_is_approve_with_no_findings():
    result = parse_review_output(json.dumps(_payload([], verdict="approve")))
    assert result.verdict == "approve"
    assert result.findings == []


def test_legacy_fallback_and_praise_dropped():
    text = "---\nFILE: x.py\nLINE: 2\nSEVERITY: major\nCOMMENT:\nbad\n---\nFILE: x.py\nLINE: 1\nSEVERITY: praise\nCOMMENT:\nnice\n---"
    result = parse_review_output(text)
    assert result.parse_mode == "legacy"
    assert [f.severity for f in result.findings] == ["major"]
    assert result.verdict == "changes_requested"


def test_legacy_unknown_severity_surfaces_problem():
    comments = parse_review_comments("FILE: x.py\nLINE: 2\nSEVERITY: critical\nCOMMENT:\nbad")
    assert comments[0].problem and not comments[0].include
    comments = parse_review_comments("FILE: x.py\nLINE: abc\nSEVERITY: nit\nCOMMENT:\nbad")
    assert "not a number" in comments[0].problem


def test_floor_and_nit_cap_and_order():
    findings = [_finding(severity="nit", title=f"n{i}") for i in range(4)] + [
        _finding(severity="minor"), _finding(severity="blocker")
    ]
    result = apply_floor(parse_review_output(json.dumps(_payload(findings))), "nit", max_nits=2)
    assert [f.severity for f in result.findings] == ["blocker", "minor", "nit", "nit"]
    assert len(result.filtered_out) == 2
    result = apply_floor(parse_review_output(json.dumps(_payload(findings))), "major")
    assert [f.severity for f in result.findings] == ["blocker"]


def test_validate_lines_marks_not_in_diff():
    diffs = {"x.py": parse_file_patch("x.py", PATCH)}
    result = parse_review_output(json.dumps(_payload([
        _finding(line=2), _finding(line=50), _finding(file="other.py"), _finding(line=None),
    ])))
    bad = validate_lines(result, diffs)
    assert len(bad) == 2
    assert result.findings[1].problem == NOT_IN_DIFF
    assert result.findings[1].include  # still posted, via the summary
    assert result.findings[0].include and result.findings[3].include


def test_fingerprint_ignores_line_and_uses_hunk_symbol():
    diff = parse_file_patch("x.py", PATCH)
    a = parse_review_output("", structured=_payload([_finding(line=2)])).findings[0]
    b = parse_review_output("", structured=_payload([_finding(line=3, title="  null DEREF ")])).findings[0]
    assert fingerprint(a, diff) == fingerprint(b, diff)
    assert len(fingerprint(a, diff)) == 12


def test_markers_round_trip():
    body = fp_marker("0123456789ab", "major", "deadbeef") + "\n**Major: x**"
    assert parse_fp_marker(body) == {"fp": "0123456789ab", "sev": "major", "sha": "deadbeef"}
    marker = review_marker("abc123", "def456")
    parsed = parse_review_marker("## Approve\n" + marker)
    assert parsed["sha"] == "abc123" and parsed["base"] == "def456"
    assert parse_review_marker("no marker") is None
