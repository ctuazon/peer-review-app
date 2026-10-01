from app.cost import budget_fit_steps, estimate_cost, model_info
from app.diff_model import parse_file_patch
from app.diff_select import GENERATED_REASON, OVER_BUDGET, WITHHELD_SECRET, render_selected, select_diffs
from app.eligibility import eligibility_warnings, title_carries
from app.repo_config import RepoConfig, parse_repo_config, resolve_settings
from app.secrets_scan import mask_diffs, mask_text, scan_diffs, scan_line

# Assembled at runtime so this file never contains a real-shaped key literal.
REAL_GH = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
REAL_AWS = "AKIA" + "QWERTYUIOPASDFGH"


def test_real_shaped_keys_match():
    assert scan_line("a", 1, f"token = '{REAL_GH}'")
    assert scan_line("a", 1, f"key: {REAL_AWS}")
    assert scan_line("a", 1, "$db_password = 'hunter2hunter2';")
    assert scan_line("a", 1, "-----BEGIN RSA PRIVATE KEY-----")


def test_placeholders_never_match():
    for line in [
        "AKIAIOSFODNN7EXAMPLE",
        "'password' => 'required|string|min:8',",
        "password: 'changeme'",
        "token = 'xxxxxxxxxxxxxxxx'",
        "url = 'https://user:secretpass@example.com/x'",
        "api_key = '${API_KEY}'",
        "api_key = '<your-api-key>'",
    ]:
        assert scan_line("a", 1, line) is None, line


def test_mask_keeps_line_numbers_and_hides_value():
    patch = f"@@ -1,1 +1,2 @@\n a\n+token = '{REAL_GH}'"
    diffs = {"a.py": parse_file_patch("a.py", patch)}
    assert scan_diffs(diffs)[0].line == 2
    masked = mask_diffs(diffs)["a.py"]
    text = masked.annotated()
    assert REAL_GH not in text
    assert "R2" in text and "ghp_" in text  # first four kept to tell keys apart
    assert REAL_GH not in mask_text(f"x {REAL_GH} y")


def test_private_key_body_masked():
    text = "-----BEGIN RSA PRIVATE KEY-----\nMIIEsecretbody\n-----END RSA PRIVATE KEY-----\nafter"
    out = mask_text(text).split("\n")
    assert "secretbody" not in out[1]
    assert out[3] == "after"


def test_select_withholds_secrets_skips_generated_and_cuts_whole_files():
    patch = "@@ -1,1 +1,2 @@\n a\n+b"
    diffs = {
        p: parse_file_patch(p, patch)
        for p in ["src/a.py", "tests/test_a.py", ".env", "package-lock.json", "src/b.py"]
    }
    sel = select_diffs(diffs, max_chars=10_000)
    assert sel.omitted[".env"] == WITHHELD_SECRET
    assert sel.omitted["package-lock.json"] == GENERATED_REASON
    assert sel.shown == ["src/a.py", "src/b.py", "tests/test_a.py"]  # source before tests
    one = len(diffs["src/a.py"].annotated()) + 2
    sel = select_diffs(diffs, max_chars=one * 2)
    assert sel.shown == ["src/a.py", "src/b.py"]
    assert sel.omitted["tests/test_a.py"] == OVER_BUDGET
    assert "# Not shown" in render_selected(diffs, sel)


def test_cost_estimate_scales_with_effort_and_verification():
    low = estimate_cost(20_000, model="claude-opus-5-5", effort="low")
    high = estimate_cost(20_000, model="claude-opus-5-5", effort="high")
    assert low < high
    assert estimate_cost(20_000, model="claude-opus-5-5", verification=True) == 2 * estimate_cost(20_000, model="claude-opus-5-5")
    assert estimate_cost(1, model="gpt-whatever") is None
    assert model_info("claude-haiku-4-5").supports_effort is False
    steps = budget_fit_steps(verification=True, agentic=True, effort="xhigh")
    assert [s[1] for s in steps] == [{"verify": False}, {"agentic": False}, {"effort": "high"}, {"effort": "medium"}]


def test_repo_config_validates_each_key_separately():
    cfg = parse_repo_config(
        """
defaults:
  model: gpt-4
  effort: high
severity_floor: important
max_nits: 2
tone_instructions: Be direct.
path_instructions:
  - path: "src/Api/**"
    instructions: Check error responses for info leaks.
  - path: "x"
tiers:
  - name: docs
    match:
      all_paths: ["docs/**"]
    model: claude-sonnet-5-5
    effort: low
triggers:
  ignore_title_keywords: ["WIP"]
  base_branches: ["main", "release/*"]
"""
    )
    assert cfg.found
    assert "model" not in cfg.defaults and cfg.defaults["effort"] == "high"
    assert cfg.severity_floor == "major" and cfg.max_nits == 2
    assert len(cfg.path_instructions) == 1
    assert any("gpt-4" in p for p in cfg.problems)
    assert any("path_instructions[1]" in p for p in cfg.problems)

    docs = resolve_settings(cfg, paths=["docs/a.md"], author="x", changed_lines=3, draft=False, desktop={})
    assert (docs.model, docs.effort, docs.tier) == ("claude-sonnet-5-5", "low", "docs")
    code = resolve_settings(cfg, paths=["src/a.py"], author="x", changed_lines=3, draft=False,
                            desktop={"model": "claude-opus-5-5"}, overrides={"effort": "max"})
    assert code.effort == "max" and code.sources["effort"] == "UI"
    assert code.sources["model"] == "desktop"


def test_bad_yaml_reports_and_keeps_defaults():
    cfg = parse_repo_config("defaults: [unclosed")
    assert cfg.problems
    assert parse_repo_config(None).found is False


def test_title_keywords_are_whole_words():
    assert title_carries("WIP: refactor", "WIP")
    assert not title_carries("Wipe stale cache", "WIP")
    assert not title_carries("WIP-1234 thing", "WIP")
    assert title_carries("Please do not review yet", "do not review")


def test_eligibility_warnings():
    warnings = eligibility_warnings(
        title="DNR experiment", draft=True, state="closed", merged=False, base_branch="dev",
        head_sha="abc1234", last_reviewed_sha="abc1234", cfg=RepoConfig(base_branches=["main"]),
    )
    assert len(warnings) == 5


def test_api_mode_without_key_falls_back_to_cli(monkeypatch):
    from app.claude_runner import api_fallback_note, effective_mode, resolve_api_key

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert effective_mode({"claude_mode": "api", "anthropic_api_key": ""}) == "cli"
    assert api_fallback_note({"claude_mode": "api"})
    assert effective_mode({"claude_mode": "api", "anthropic_api_key": "sk-x"}) == "api"
    assert effective_mode({"claude_mode": "cli", "anthropic_api_key": "sk-x"}) == "cli"
    assert api_fallback_note({"claude_mode": "cli"}) == ""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env")
    assert resolve_api_key({"claude_mode": "api"}) == "sk-env"
    assert effective_mode({"claude_mode": "api"}) == "api"


def test_run_claude_uses_cli_when_api_key_missing(monkeypatch):
    from app import claude_runner

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    calls = []
    monkeypatch.setattr(claude_runner, "run_claude_api_result", lambda **kw: calls.append("api"))
    monkeypatch.setattr(
        claude_runner, "run_claude_cli_any",
        lambda prompt, config, on_event, options: calls.append("cli") or claude_runner.ClaudeResult("ok"),
    )
    events = []
    assert claude_runner.run_claude("hi", {"claude_mode": "api"}, on_event=events.append) == "ok"
    assert calls == ["cli"]
    assert any("no Anthropic API key" in e.get("text", "") for e in events)
