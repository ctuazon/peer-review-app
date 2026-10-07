import json

from app.claude_runner import _parse_stream_line, _StreamState
from app.diff_model import parse_file_patch
from app.diff_select import OVER_BUDGET, select_diffs
from app import history_store
from app.history_store import HistoryEntry, list_reviews_for_pr
from app.prompts_store import Prompt
from app.repo_config import parse_repo_config, resolve_settings
from app.review import RAW_TEXT_LIMIT, ReviewRun, review_to_history
from app.review_prompts import RUBRIC
from app.review_schema import INCONCLUSIVE, ReviewResult, parse_review_output, review_schema

PR = "https://github.com/acme/app/pull/6"


def test_unreadable_output_is_inconclusive_never_approve():
    narration = "I'll start by reading CLAUDE.md and AGENTS.md, then trace the budget flow."
    result = parse_review_output(narration)
    assert result.verdict == INCONCLUSIVE
    assert result.headline().startswith("Inconclusive")
    assert result.raw_text == narration


def test_legacy_blocks_still_decide_the_verdict():
    result = parse_review_output("FILE: x.py\nLINE: 2\nSEVERITY: major\nCOMMENT:\nbad")
    assert result.parse_mode == "legacy"
    assert result.verdict == "changes_requested"


def test_report_has_a_schema_field_and_reaches_history():
    assert "report" in review_schema()["properties"]
    payload = {"verdict": "approve", "verdict_reason": "", "scope_note": "", "tests_note": "", "findings": [],
               "prior_findings": [], "disagreements": [], "credits": [], "report": "| Constraint | Holds |"}
    result = parse_review_output("", structured=payload)
    assert result.report == "| Constraint | Holds |"
    assert result.to_dict()["report"] == "| Constraint | Holds |"


def test_rubric_routes_report_sections_and_admits_tests_were_not_run():
    assert "`report`" in RUBRIC
    assert "You cannot run commands" in RUBRIC


def test_history_keeps_the_raw_output_capped():
    run = ReviewRun(prep=None, result=ReviewResult(verdict=INCONCLUSIVE), raw_text="x" * (RAW_TEXT_LIMIT + 10))
    data = review_to_history(run)
    assert len(data["raw_text"]) == RAW_TEXT_LIMIT
    assert data["verdict"] == INCONCLUSIVE


def test_a_follow_up_never_treats_an_inconclusive_run_as_a_pass(monkeypatch):
    def entry(verdict):
        return HistoryEntry(id=verdict, created_at="", mode="review", pr_url=PR, result="r", review={"verdict": verdict})

    monkeypatch.setattr(history_store, "list_history", lambda: [entry(INCONCLUSIVE), entry("approve")])
    assert [e.id for e in list_reviews_for_pr(PR)] == ["approve"]


def test_errored_cli_run_reports_why_it_stopped():
    state = _StreamState()
    line = json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True, "result": ""})
    _parse_stream_line(line, None, [], state)
    assert state.is_error
    assert state.stop_reason == "error_max_turns"


def test_files_over_the_budget_are_named_for_the_partial_review_warning():
    patch = "@@ -1,1 +1,2 @@\n a\n+b"
    diffs = {p: parse_file_patch(p, patch) for p in ["src/a.py", "src/b.py", "src/c.py"]}
    one = len(diffs["src/a.py"].annotated()) + 2
    selection = select_diffs(diffs, max_chars=one)
    assert selection.over_budget == ["src/b.py", "src/c.py"]
    assert all(selection.omitted[p] == OVER_BUDGET for p in selection.over_budget)


def _settings(min_effort, overrides=None, effort="medium"):
    return resolve_settings(
        parse_repo_config(None), paths=["a.php"], author="dev", changed_lines=10, draft=False,
        desktop={"model": "claude-opus-5-5", "effort": effort}, overrides=overrides, min_effort=min_effort,
    )


def test_prompt_minimum_effort_raises_a_lower_effort():
    settings = _settings("high")
    assert settings.effort == "high"
    assert settings.sources["effort"] == "reviewer prompt minimum"


def test_prompt_minimum_effort_never_lowers_or_beats_the_ui():
    assert _settings("high", effort="xhigh").effort == "xhigh"
    assert _settings("high", overrides={"effort": "low"}).effort == "low"
    assert _settings("").effort == "medium"


def test_prompt_minimum_effort_ignores_unknown_values():
    prompt = Prompt.from_dict({"name": "p", "content": "c", "min_effort": "extreme"})
    assert prompt.min_effort == ""
    assert Prompt.from_dict({"name": "p", "content": "c", "min_effort": "High"}).min_effort == "high"
