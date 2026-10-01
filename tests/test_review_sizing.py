from app import DEFAULT_CONFIG
from app.diff_model import parse_file_patch
from app.diff_select import render_delta, review_delta, select_diffs
from app.repo_config import parse_repo_config, pick_size_tier, resolve_settings
from app.review_prompts import build_review_parts

TIERS = DEFAULT_CONFIG["review_size_tiers"]
DESKTOP = {"model": "claude-opus-5-5", "effort": "high", "size_tiers": TIERS}


def _resolve(lines, cfg=None, overrides=None, desktop=DESKTOP):
    return resolve_settings(
        cfg or parse_repo_config(None), paths=["a.php"], author="dev", changed_lines=lines, draft=False,
        desktop=desktop, overrides=overrides, review_lines=lines,
    )


def test_size_tiers_pick_by_lines_and_keep_agentic():
    small, medium, large = _resolve(40), _resolve(900), _resolve(5000)
    assert (small.model, small.effort, small.tier) == ("claude-sonnet-5-5", "low", "small")
    assert (medium.model, medium.effort) == ("claude-sonnet-5-5", "medium")
    assert (large.model, large.effort) == ("claude-opus-5-5", "medium")
    assert {small.mode, medium.mode, large.mode} == {"agentic"}
    assert "size tier 'small'" in small.sources["model"]


def test_size_tiers_off_uses_desktop_defaults():
    out = _resolve(40, desktop={"model": "claude-opus-5-5", "effort": "high", "size_tiers": None})
    assert (out.model, out.effort, out.tier) == ("claude-opus-5-5", "high", "default")


def test_yml_and_ui_still_beat_size_tiers():
    cfg = parse_repo_config("defaults:\n  model: claude-fable-5-1\n")
    assert _resolve(40, cfg=cfg).model == "claude-fable-5-1"
    assert _resolve(40, overrides={"effort": "max"}).effort == "max"


def test_invalid_size_tier_is_skipped():
    tiers = [{"max_lines": 100, "model": "nope", "effort": "low"}, {"max_lines": None, "model": "claude-sonnet-5-5", "effort": "medium"}]
    assert pick_size_tier(tiers, 10)["model"] == "claude-sonnet-5-5"
    assert pick_size_tier("not a list", 10) is None


FULL = "@@ -1,2 +1,6 @@\n keep\n+a1\n+a2\n+a3\n+a4\n keep2"
DELTA = "@@ -3,2 +3,3 @@\n+a2\n+fix\n a3"


def test_re_review_sends_only_the_delta_for_pr_files():
    diffs = {"a.php": parse_file_patch("a.php", FULL), "b.php": parse_file_patch("b.php", FULL)}
    selection = select_diffs(diffs)
    since = {"a.php": parse_file_patch("a.php", DELTA), "base-merge.php": parse_file_patch("base-merge.php", DELTA)}
    delta = review_delta(since, diffs, selection)
    assert list(delta) == ["a.php"]  # files outside the PR (a base merge) are dropped
    assert sum(d.changed_lines for d in delta.values()) == 2
    text = render_delta(delta, selection, "abc1234def")
    assert "+fix" in text and "abc1234" in text
    assert "# Unchanged since your last review\n\n- b.php" in text


def test_delta_falls_back_to_full_diff():
    diffs = {"a.php": parse_file_patch("a.php", DELTA)}
    selection = select_diffs(diffs)
    assert review_delta(None, diffs, selection) is None  # force-pushed away
    bigger = {"a.php": parse_file_patch("a.php", FULL)}
    assert review_delta(bigger, diffs, selection) is None  # delta no smaller than the PR


def test_delta_only_prompt_heading():
    common = dict(
        focus_name="x", focus_prompt="", conventions="", parameters="p", pr_header="h", description="",
        requester_notes="", tickets="", ci="", history="", security="", other_reviewers="", diff="DIFF",
    )
    assert build_review_parts(**common, delta_only=True).user[-1].text.startswith("# Changed since your last review")
    assert build_review_parts(**common).user[-1].text.startswith("# The diff")
