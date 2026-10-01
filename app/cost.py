"""Model catalogue, pricing and the pre-flight cost estimate.

Prices are Anthropic first-party list prices per million tokens (cached
2026-09-25). The estimate follows the Laravel app's CostEstimator: output is
calibrated from measured runs and scaled by effort and mode.
"""
from __future__ import annotations

from dataclasses import dataclass

DEFAULT_REVIEW_MODEL = "claude-opus-5-5"
DEFAULT_TRIAGE_MODEL = "claude-haiku-4-5-20251001"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_EFFORT = "medium"


@dataclass(frozen=True)
class ModelInfo:
    id: str
    label: str
    input: float
    output: float
    cache_read: float
    supports_effort: bool = True
    # Fable reasons on every request and rejects an explicit thinking config;
    # Haiku 4.5 predates adaptive thinking.
    adaptive_thinking: bool = True
    send_thinking: bool = True

    @property
    def cache_write(self) -> float:
        return self.input * 1.25


MODELS: dict[str, ModelInfo] = {
    m.id: m
    for m in (
        ModelInfo("claude-opus-5-5", "Opus 5.5 (default)", 4.00, 20.00, 0.20),
        ModelInfo("claude-sonnet-5-5", "Sonnet 5.5 (cheaper)", 2.00, 10.00, 0.20),
        ModelInfo("claude-fable-5-1", "Fable 5.1 (deepest)", 10.00, 50.00, 0.25, send_thinking=False),
        ModelInfo(
            "claude-haiku-4-5-20251001", "Haiku 4.5 (triage)", 1.00, 5.00, 0.10,
            supports_effort=False, adaptive_thinking=False, send_thinking=False,
        ),
    )
}
MODEL_ALIASES = {"claude-haiku-4-5": "claude-haiku-4-5-20251001"}
REVIEW_MODEL_CHOICES = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1"]

EFFORT_OUTPUT_MULTIPLIER = {"low": 0.4, "medium": 1.0, "high": 1.8, "xhigh": 2.6, "max": 4.0}
# Output a medium-effort single-shot review generates, thinking included.
BASE_OUTPUT_TOKENS = 5200
AGENTIC_OUTPUT_CEILING = 2.35
AGENTIC_OUTPUT_SATURATES_AT = 100_000
CHARS_PER_TOKEN = 3.5


def model_info(model_id: str) -> ModelInfo | None:
    return MODELS.get(MODEL_ALIASES.get(model_id, model_id))


def estimate_tokens(text: str) -> int:
    return int(len(text or "") / CHARS_PER_TOKEN) + 1


def expected_turns(input_tokens: int) -> int:
    return round(max(2, min(16, 2.3 + input_tokens / 10_000)))


def estimate_cost(
    input_tokens: int,
    *,
    model: str,
    effort: str = DEFAULT_EFFORT,
    agentic: bool = False,
    verification: bool = False,
) -> float | None:
    """Dollars for one review run; None for an unpriced model."""
    info = model_info(model)
    if info is None:
        return None
    multiplier = EFFORT_OUTPUT_MULTIPLIER.get(effort, 1.0) if info.supports_effort else 1.0
    factor = max(1.0, AGENTIC_OUTPUT_CEILING * min(1.0, input_tokens / AGENTIC_OUTPUT_SATURATES_AT)) if agentic else 1.0
    output_tokens = BASE_OUTPUT_TOKENS * multiplier * factor
    cache_reads = input_tokens * (expected_turns(input_tokens) - 1) if agentic else 0
    cost = (
        input_tokens * info.cache_write + cache_reads * info.cache_read + output_tokens * info.output
    ) / 1_000_000
    return cost * 2 if verification else cost


def actual_cost(
    model: str, input_tokens: int, output_tokens: int, cache_read: int = 0, cache_write: int = 0
) -> float | None:
    info = model_info(model)
    if info is None:
        return None
    return (
        input_tokens * info.input
        + output_tokens * info.output
        + cache_read * info.cache_read
        + cache_write * info.cache_write
    ) / 1_000_000


def budget_fit_steps(*, verification: bool, agentic: bool, effort: str) -> list[tuple[str, dict]]:
    """The Laravel downgrade order, as one-click options: each is (label, changes)."""
    steps: list[tuple[str, dict]] = []
    if verification:
        steps.append(("Drop the verification pass", {"verify": False}))
    if agentic:
        steps.append(("Switch to single-shot (no repo access)", {"agentic": False}))
    order = list(EFFORTS)
    if effort in order:
        for lower in reversed(order[: order.index(effort)]):
            if lower in ("low",):
                break
            steps.append((f"Lower effort to {lower}", {"effort": lower}))
    return steps
