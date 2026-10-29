"""PolicyEngine: decides in code whether a tool call runs, needs a person, or is refused.

GPT cannot grant itself a capability: prompts describe the rules, this module enforces
them (SECURITY T5, T9).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.db.models import TERMINAL_RUN_STATES, RunState
from app.tools.base import RiskLevel, ToolSpec

# SECURITY.md §3. Phase 8 adds the N8N_AUTO_APPROVE exception for EXTERNAL_NETWORK.
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
    def decide(self, spec: ToolSpec, state: RunState, *, approved: bool = False) -> PolicyDecision:
        if state in TERMINAL_RUN_STATES or state in (RunState.AWAITING_APPROVAL, RunState.CREATED):
            return PolicyDecision(Decision.DENY, f"no tool may run while the run is {state}")
        if spec.states is not None and state not in spec.states:
            allowed = ", ".join(sorted(s.value for s in spec.states))
            return PolicyDecision(Decision.DENY, f"{spec.name} is not available in state {state} (allowed: {allowed})")
        if spec.asks_human:
            return PolicyDecision(Decision.ASK_HUMAN, "the run pauses until a person answers")
        if spec.risk in _ALWAYS_APPROVE and not approved:
            return PolicyDecision(Decision.REQUIRE_APPROVAL, f"{spec.risk} actions always need approval")
        return PolicyDecision(Decision.ALLOW, f"{spec.risk} is allowed in {state}")
