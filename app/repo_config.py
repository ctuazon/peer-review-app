"""Read the per-repo `.github/pr-review.yml` the Laravel app uses, so both
apps behave the same on a given repo.

Only the subset that applies to a manual desktop review is read. Unlike the
Laravel parser, each key is validated on its own: one bad key is reported and
ignored, the rest still apply.

Precedence for every setting: UI override, then pr-review.yml (a matching
tier, then `defaults`), then the desktop defaults. `ResolvedSettings.sources`
records where each value came from, for the Inspect view.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.cost import DEFAULT_EFFORT, DEFAULT_REVIEW_MODEL, EFFORTS, model_info
from app.globs import matches, matches_any
from app.review_schema import SEVERITIES

CONFIG_PATH = ".github/pr-review.yml"
MODES = ("agentic", "single-shot")
MAX_TONE_CHARS = 250
DEFAULT_IGNORE_KEYWORDS = ["WIP", "do not review", "DNR"]
DEFAULT_MAX_NITS = 5
DEFAULT_FOLLOW_UP_FLOOR = "major"


@dataclass
class PathInstruction:
    path: str
    instructions: str

    def matching(self, paths: list[str]) -> list[str]:
        return [p for p in paths if matches(self.path, p)]


@dataclass
class Tier:
    name: str
    any_paths: list[str] = field(default_factory=list)
    all_paths: list[str] = field(default_factory=list)
    authors: list[str] = field(default_factory=list)
    max_changed_lines: int | None = None
    draft: bool | None = None
    skip: bool = False
    model: str | None = None
    effort: str | None = None
    mode: str | None = None
    verification_pass: bool | None = None

    def matches(self, *, paths: list[str], author: str, changed_lines: int, draft: bool) -> bool:
        if self.any_paths and not any(matches_any(self.any_paths, p) for p in paths):
            return False
        if self.all_paths and not (paths and all(matches_any(self.all_paths, p) for p in paths)):
            return False
        if self.authors and author not in self.authors:
            return False
        if self.max_changed_lines is not None and changed_lines > self.max_changed_lines:
            return False
        if self.draft is not None and draft != self.draft:
            return False
        return True


@dataclass
class RepoConfig:
    found: bool = False
    defaults: dict[str, Any] = field(default_factory=dict)
    tiers: list[Tier] = field(default_factory=list)
    path_instructions: list[PathInstruction] = field(default_factory=list)
    tone_instructions: str = ""
    language: str = ""
    severity_floor: str | None = None
    max_nits: int | None = None
    follow_up_floor: str | None = None
    ignore_title_keywords: list[str] | None = None
    base_branches: list[str] | None = None
    exclude_globs: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value if isinstance(v, (str, int, float))]
    return []


class _Parser:
    def __init__(self) -> None:
        self.problems: list[str] = []

    def model(self, value: Any, where: str) -> str | None:
        if value in (None, ""):
            return None
        if model_info(str(value)) is None:
            self.problems.append(f"{where}: unknown model '{value}', ignored")
            return None
        return str(value)

    def effort(self, value: Any, where: str) -> str | None:
        if value in (None, ""):
            return None
        if str(value) not in EFFORTS:
            self.problems.append(f"{where}: unknown effort '{value}', expected one of {', '.join(EFFORTS)}")
            return None
        return str(value)

    def mode(self, value: Any, where: str) -> str | None:
        if value in (None, ""):
            return None
        if str(value) not in MODES:
            self.problems.append(f"{where}: unknown mode '{value}', expected agentic or single-shot")
            return None
        return str(value)

    def severity(self, value: Any, where: str) -> str | None:
        if value in (None, ""):
            return None
        text = "major" if str(value) == "important" else str(value)
        if text not in SEVERITIES:
            self.problems.append(f"{where}: unknown severity '{value}', expected one of {', '.join(SEVERITIES)}")
            return None
        return text

    def bool(self, value: Any, where: str) -> bool | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        self.problems.append(f"{where} must be true or false")
        return None


def parse_repo_config(text: str | None) -> RepoConfig:
    if text is None:
        return RepoConfig()
    cfg = RepoConfig(found=True)
    try:
        import yaml

        raw = yaml.safe_load(text) or {}
    except Exception as exc:  # noqa: BLE001 -- yaml errors vary by version
        cfg.problems.append(f"{CONFIG_PATH} does not parse: {exc}")
        return cfg
    if not isinstance(raw, dict):
        cfg.problems.append(f"{CONFIG_PATH} must be a mapping at the top level")
        return cfg

    p = _Parser()
    defaults = raw.get("defaults")
    if isinstance(defaults, dict):
        for key, parsed in (
            ("model", p.model(defaults.get("model"), "defaults.model")),
            ("effort", p.effort(defaults.get("effort"), "defaults.effort")),
            ("mode", p.mode(defaults.get("mode"), "defaults.mode")),
            ("verification_pass", p.bool(defaults.get("verification_pass"), "defaults.verification_pass")),
        ):
            if parsed is not None:
                cfg.defaults[key] = parsed
    elif defaults is not None:
        p.problems.append("defaults must be a mapping")

    tiers = raw.get("tiers")
    if isinstance(tiers, list):
        for index, entry in enumerate(tiers):
            if not isinstance(entry, dict):
                p.problems.append(f"tiers[{index}] must be a mapping")
                continue
            name = str(entry.get("name") or f"tier-{index}")
            match = entry.get("match") if isinstance(entry.get("match"), dict) else {}
            tier = Tier(
                name=name,
                any_paths=_str_list(match.get("any_path", match.get("paths", []))),
                all_paths=_str_list(match.get("all_paths", [])),
                authors=_str_list(match.get("authors", [])),
                max_changed_lines=int(match["max_changed_lines"]) if str(match.get("max_changed_lines", "")).isdigit() else None,
                draft=match.get("draft") if isinstance(match.get("draft"), bool) else None,
                skip=str(entry.get("action") or "review") == "skip",
                model=p.model(entry.get("model"), f"tier '{name}'"),
                effort=p.effort(entry.get("effort"), f"tier '{name}'"),
                mode=p.mode(entry.get("mode"), f"tier '{name}'"),
                verification_pass=p.bool(entry.get("verification_pass"), f"tier '{name}'.verification_pass"),
            )
            if not (tier.any_paths or tier.all_paths or tier.authors or tier.max_changed_lines is not None or tier.draft is not None):
                p.problems.append(f"tier '{name}' has no match criteria the desktop understands, ignored")
                continue
            cfg.tiers.append(tier)
    elif tiers is not None:
        p.problems.append("tiers must be a list")

    path_instructions = raw.get("path_instructions")
    if isinstance(path_instructions, list):
        for index, entry in enumerate(path_instructions):
            if isinstance(entry, dict) and entry.get("path") and entry.get("instructions"):
                cfg.path_instructions.append(PathInstruction(str(entry["path"]), str(entry["instructions"]).strip()))
            else:
                p.problems.append(f"path_instructions[{index}] needs both a path and instructions")
    elif path_instructions is not None:
        p.problems.append("path_instructions must be a list")

    tone = raw.get("tone_instructions")
    if isinstance(tone, str):
        if len(tone) > MAX_TONE_CHARS:
            p.problems.append(f"tone_instructions is longer than {MAX_TONE_CHARS} characters, ignored")
        else:
            cfg.tone_instructions = tone.strip()

    if isinstance(raw.get("language"), str):
        cfg.language = raw["language"].strip()

    cfg.severity_floor = p.severity(raw.get("severity_floor"), "severity_floor")
    if raw.get("max_nits") is not None:
        try:
            cfg.max_nits = max(0, int(raw["max_nits"]))
        except (TypeError, ValueError):
            p.problems.append("max_nits must be a number")
    follow_up = raw.get("follow_up")
    if isinstance(follow_up, dict):
        cfg.follow_up_floor = p.severity(follow_up.get("severity_floor"), "follow_up.severity_floor")

    triggers = raw.get("triggers")
    if isinstance(triggers, dict):
        if "ignore_title_keywords" in triggers:
            cfg.ignore_title_keywords = _str_list(triggers["ignore_title_keywords"])
        if "base_branches" in triggers:
            cfg.base_branches = _str_list(triggers["base_branches"])

    context = raw.get("context")
    if isinstance(context, dict):
        cfg.exclude_globs = _str_list(context.get("exclude_globs", []))

    cfg.problems = p.problems
    return cfg


@dataclass
class ResolvedSettings:
    model: str = DEFAULT_REVIEW_MODEL
    effort: str = DEFAULT_EFFORT
    mode: str = "agentic"
    verify: bool = False
    tier: str = "default"
    skip_reason: str = ""
    severity_floor: str = "nit"
    max_nits: int = DEFAULT_MAX_NITS
    follow_up_floor: str = DEFAULT_FOLLOW_UP_FLOOR
    sources: dict[str, str] = field(default_factory=dict)


def resolve_settings(
    cfg: RepoConfig,
    *,
    paths: list[str],
    author: str,
    changed_lines: int,
    draft: bool,
    desktop: dict[str, Any],
    overrides: dict[str, Any] | None = None,
) -> ResolvedSettings:
    """Merge desktop defaults < yml defaults < matching tier < UI overrides."""
    out = ResolvedSettings()
    values: dict[str, tuple[Any, str]] = {
        "model": (desktop.get("model") or DEFAULT_REVIEW_MODEL, "desktop"),
        "effort": (desktop.get("effort") or DEFAULT_EFFORT, "desktop"),
        "mode": (desktop.get("mode") or "agentic", "desktop"),
        "verify": (bool(desktop.get("verify", False)), "desktop"),
    }
    for key in ("model", "effort", "mode"):
        if key in cfg.defaults:
            values[key] = (cfg.defaults[key], "pr-review.yml defaults")
    if "verification_pass" in cfg.defaults:
        values["verify"] = (cfg.defaults["verification_pass"], "pr-review.yml defaults")

    for tier in cfg.tiers:
        if tier.matches(paths=paths, author=author, changed_lines=changed_lines, draft=draft):
            out.tier = tier.name
            where = f"pr-review.yml tier '{tier.name}'"
            if tier.skip:
                out.skip_reason = f"tier '{tier.name}' says skip"
            for key, value in (("model", tier.model), ("effort", tier.effort), ("mode", tier.mode), ("verify", tier.verification_pass)):
                if value is not None:
                    values[key] = (value, where)
            break

    for key, value in (overrides or {}).items():
        if key in values and value not in (None, ""):
            values[key] = (value, "UI")

    out.model, out.effort, out.mode = values["model"][0], values["effort"][0], values["mode"][0]
    out.verify = bool(values["verify"][0])
    out.sources = {key: src for key, (_v, src) in values.items()}

    if cfg.severity_floor:
        out.severity_floor = cfg.severity_floor
        out.sources["severity_floor"] = "pr-review.yml"
    if cfg.max_nits is not None:
        out.max_nits = cfg.max_nits
        out.sources["max_nits"] = "pr-review.yml"
    if cfg.follow_up_floor:
        out.follow_up_floor = cfg.follow_up_floor
        out.sources["follow_up_floor"] = "pr-review.yml"
    return out
