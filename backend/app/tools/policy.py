"""PolicyEngine: decides in code whether a tool call runs, needs a person, or is refused.

GPT cannot grant itself a capability: prompts describe the rules, this module enforces
them (SECURITY T5, T9).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.db.models import TERMINAL_RUN_STATES, RunState
from app.tools.base import RiskLevel, ToolSpec

# SECURITY.md §3. The one exception: an EXTERNAL_NETWORK call whose tool's auto_approve accepts its arguments (N8N_AUTO_APPROVE=true,
# for an n8n workflow declared read_only or notify). Without the parsed arguments the exception never applies.
_ALWAYS_APPROVE = frozenset({RiskLevel.EXTERNAL_NETWORK, RiskLevel.GIT_PUSH, RiskLevel.DESTRUCTIVE})


class Decision(StrEnum):
    ALLOW = "ALLOW"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    ASK_HUMAN = "ASK_HUMAN"
    DENY = "DENY"


@dataclass(frozen=True)
class PolicyDecision:
    decision: Decision
    reason: str


class PolicyEngine:
    def decide(self, spec: ToolSpec, state: RunState, *, approved: bool = False, args: Any = None) -> PolicyDecision:
        if state in TERMINAL_RUN_STATES or state in (RunState.AWAITING_APPROVAL, RunState.CREATED):
            return PolicyDecision(Decision.DENY, f"no tool may run while the run is {state}")
        if spec.states is not None and state not in spec.states:
            allowed = ", ".join(sorted(s.value for s in spec.states))
            return PolicyDecision(Decision.DENY, f"{spec.name} is not available in state {state} (allowed: {allowed})")
        if spec.asks_human:
            return PolicyDecision(Decision.ASK_HUMAN, "the run pauses until a person answers")
        if spec.auto_approve is not None and spec.risk is RiskLevel.EXTERNAL_NETWORK and not approved and args is not None and _exempt(spec, args):
            return PolicyDecision(Decision.ALLOW, f"{spec.name} runs without approval: N8N_AUTO_APPROVE=true and this call has no side effect beyond a notification")
        if spec.risk in _ALWAYS_APPROVE and not approved:
            return PolicyDecision(Decision.REQUIRE_APPROVAL, f"{spec.risk} actions always need approval")
        return PolicyDecision(Decision.ALLOW, f"{spec.risk} is allowed in {state}")


def _exempt(spec: ToolSpec, args: Any) -> bool:
    """Fails closed: a predicate that errors means the call waits for a person."""
    assert spec.auto_approve is not None
    try:
        return bool(spec.auto_approve(args))
    except Exception:  # noqa: BLE001
        return False
