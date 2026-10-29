"""Run limits enforced in code: turns, tool calls, tokens, spend and active time."""

from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings
from app.db.models import Run
from app.orchestrator.llm_client import Usage


@dataclass(frozen=True)
class LimitHit:
    reason: str  # stable code, stored as the run outcome
    detail: str


def turn_cost_usd(usage: Usage, settings: Settings) -> float | None:
    """Cost of one GPT turn, or None when prices are not configured."""
    if not settings.cost_known:
        return None
    assert settings.openai_price_input_per_mtok is not None and settings.openai_price_output_per_mtok is not None
    return (
        usage.input_tokens * settings.openai_price_input_per_mtok
        + usage.output_tokens * settings.openai_price_output_per_mtok
    ) / 1_000_000


class RunGuard:
    def __init__(self, settings: Settings) -> None:
        self._s = settings

    def before_llm(self, run: Run, active_seconds: float) -> LimitHit | None:
        s = self._s
        if run.turns >= s.max_turns:
            return LimitHit("turn_limit", f"MAX_TURNS={s.max_turns} reached")
        tokens = run.tokens_in + run.tokens_out
        if tokens >= s.run_max_tokens:
            return LimitHit("token_limit", f"RUN_MAX_TOKENS={s.run_max_tokens} reached ({tokens} used)")
        if run.cost_known and run.cost_usd >= s.run_budget_usd:
            return LimitHit("budget_limit", f"RUN_BUDGET_USD={s.run_budget_usd:.2f} reached (${run.cost_usd:.4f} spent)")
        return self._wall_clock(active_seconds)

    def before_tool(self, run: Run, active_seconds: float) -> LimitHit | None:
        if run.tool_calls >= self._s.max_tool_calls:
            return LimitHit("tool_call_limit", f"MAX_TOOL_CALLS={self._s.max_tool_calls} reached")
        return self._wall_clock(active_seconds)

    def _wall_clock(self, active_seconds: float) -> LimitHit | None:
        if active_seconds >= self._s.run_wall_clock_seconds:
            return LimitHit(
                "wall_clock_limit", f"RUN_WALL_CLOCK_SECONDS={self._s.run_wall_clock_seconds:g} of active time reached"
            )
        return None
