"""Run limits enforced in code: turns, tool calls, tokens, spend and active time.

Spending is strict. Before each GPT call the most it could cost is worked out and saved, and a call
that could pass the budget is not sent. Whether a call was billed is only known from its answer, so an
answer that never arrives is charged in full. The same is true of the model gateway, and both draw on
`run.cost_usd`, so the ceiling holds across GPT and OpenCode together.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from app.config import Settings
from app.db.models import Run
from app.orchestrator.llm_client import LLMRequest, Usage

# What the API adds around a request. A token covers at least one byte, so the request's size bounds the rest.
_INPUT_OVERHEAD_TOKENS = 512
# Above this many input tokens the model costs twice as much (developers.openai.com/api/docs/pricing), so the
# configured prices would not hold. edge/gateway_policy.py refuses the same requests for OpenCode.
SHORT_CONTEXT_TOKENS = 272_000


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


def turn_input_tokens(request: LLMRequest, run: Run) -> int:
    """The most input this GPT call can be billed for: everything stored at OpenAI so far, plus this request."""
    size = len(json.dumps([request.instructions, request.input, request.tools], ensure_ascii=False).encode("utf-8"))
    return run.context_tokens + size + _INPUT_OVERHEAD_TOKENS


def turn_ceiling_usd(request: LLMRequest, run: Run, settings: Settings) -> float:
    """The most this GPT call can cost. The output is at most the cap that is sent with it. Zero when prices
    are not configured."""
    return turn_cost_usd(Usage(input_tokens=turn_input_tokens(request, run), output_tokens=settings.openai_max_output_tokens or 0), settings) or 0.0


class RunGuard:
    def __init__(self, settings: Settings) -> None:
        self._s = settings

    def before_send(self, run: Run, ceiling_usd: float, input_tokens: int) -> LimitHit | None:
        """The call that could pass the budget is not sent, however little it may really cost."""
        if run.cost_known and input_tokens > SHORT_CONTEXT_TOKENS:
            return LimitHit("budget_limit", f"this call could be billed for {input_tokens} input tokens, past the {SHORT_CONTEXT_TOKENS} the prices hold for")
        if run.cost_known and run.cost_usd + ceiling_usd > self._s.run_budget_usd:
            detail = f"RUN_BUDGET_USD={self._s.run_budget_usd:.2f}: ${run.cost_usd:.4f} is spent and this call could cost ${ceiling_usd:.4f}"
            return LimitHit("budget_limit", detail)
        return None

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
