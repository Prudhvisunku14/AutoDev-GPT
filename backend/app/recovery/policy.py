"""The intelligent recovery decision: a deterministic policy, not another orchestrator (ARCHITECTURE §9.2).

GPT chooses how to fix a failure. This decides, in code, whether another try is allowed and of what kind.

1. A diagnosis that is not safe stops the run. Nothing below can change that.
2. The limits of the bounded policy still hold: this never takes more attempts or repairs than it would.
3. Each action the diagnosis offers is then tried in order, and the first one allowed now is taken:
   - Nothing runs without an eligible tool, and the Stage 2 selector says whether there is one. So the
     budget, the tokens, the security rules and the tools available all count, by the same rules.
   - An action needs the time an attempt took.
   - The same failure again means the last action did not work, so only SWITCH_TOOL is left.
   - SWITCH_TOOL needs another eligible tool. With one tool it is never chosen, so none is claimed.
   - RECONFIGURE needs approved headroom. It moves one task's attempt time limit, and nothing else.
   - REPAIR is skipped when verified history shows repairs of this failure rarely pass.
4. If nothing is left, the run stops, and says why.

The same inputs always give the same decision. This module reads no database and starts nothing.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING

from app.recovery.diagnosis import CODING, VERIFICATION, Diagnosis
from app.recovery.engine import Action, Decision, hint_for
from app.selection.adaptive import reliability

if TYPE_CHECKING:
    from app.config import Settings

_NOT_SAFE = {
    "SECURITY_VIOLATION": "a security or permission failure is never retried",
    "AMBIGUOUS_STATE": "what happened cannot be proved, so nothing more is run until a person has looked",
}
_HINTS = {
    Action.RETRY: "Call generate_code for the same task again.",
    Action.SWITCH_TOOL: "Call generate_code for the same task again. Python gives it to another tool.",
    Action.RECONFIGURE: "Call generate_code for the same task again. Python raised this task's time limit.",
}
_TEST_AGAIN_HINT = "Call submit_implementation again. The code did not change."


@dataclass(frozen=True)
class RecoveryConfig:
    repeat_limit: int = 2  # the same failure this many times in a row: the action taken after it does not work
    min_repair_rate: float = 0.25
    min_repair_samples: int = 5
    alpha: float = 2.0  # the prior for the repair rate, as for a tool's reliability
    beta: float = 2.0

    @classmethod
    def from_settings(cls, settings: Settings) -> RecoveryConfig:
        return cls(
            repeat_limit=settings.recovery_repeat_limit, min_repair_rate=settings.recovery_min_repair_rate, min_repair_samples=settings.recovery_min_repair_samples,
            alpha=settings.selection_reliability_alpha, beta=settings.selection_reliability_beta,
        )  # fmt: skip


@dataclass(frozen=True)
class ToolOptions:
    """What the selector says can run next. `real_alternative` keeps a switch between simulations from reading as a real one."""

    can_run: bool = True
    alternatives: tuple[str, ...] = ()  # eligible tools, best first, that have not failed here
    real_alternative: bool = False
    why: str = ""


@dataclass(frozen=True)
class RecoveryState:
    attempts_used: int
    max_attempts: int
    repairs_used: int
    max_repairs: int
    streak: int = 1  # how many failures in a row, this one included, have the same fingerprint
    tools: ToolOptions = ToolOptions()
    repairs_judged: int = 0  # repairs of this failure that verification has judged
    repairs_verified: int = 0  # how many of them passed
    seconds_left: float | None = None
    seconds_needed: float = 0.0  # what the last attempt took
    next_timeout: float | None = None  # the longer limit that is approved, if any


@dataclass(frozen=True)
class RecoveryMemory:
    """What the next failure or attempt must know about the last one. Saved on the run."""

    fingerprint: str = ""
    streak: int = 0
    task: int | None = None  # the task this applies to. None: all of them, as after a failed verification
    action: str = ""
    category: str = ""
    evidence: str = ""
    timeout_seconds: float | None = None  # the raised limit for `task`

    @classmethod
    def load(cls, saved: str | None) -> RecoveryMemory:
        return cls(**json.loads(saved)) if saved else cls()

    def dump(self) -> str:
        return json.dumps(asdict(self))

    def streak_after(self, diagnosis: Diagnosis) -> int:
        return self.streak + 1 if diagnosis.fingerprint == self.fingerprint else 1

    def after(self, diagnosis: Diagnosis, decision: Decision, *, task: int | None) -> RecoveryMemory:
        timeout = decision.timeout_seconds or (self.timeout_seconds if task == self.task else None)
        return RecoveryMemory(diagnosis.fingerprint, self.streak_after(diagnosis), task, decision.action.value, diagnosis.category.value, " ".join(diagnosis.evidence)[:500], timeout)

    def applies_to(self, task: int) -> bool:
        return bool(self.action) and self.task in (None, task)


def longer_timeout(current: float, *, ceiling: float, seconds_left: float | None) -> float | None:
    """Twice the limit, inside the approved ceiling and the time the run has. None when that is no longer."""
    longer = min(current * 2, ceiling, current * 2 if seconds_left is None else seconds_left)
    return longer if longer > current else None


def _limit_reached(diagnosis: Diagnosis, state: RecoveryState) -> str | None:
    if diagnosis.source == CODING and state.attempts_used >= state.max_attempts:
        return f"the limit of {state.max_attempts} attempts for this task is used up"
    if diagnosis.source == VERIFICATION and Action.REPAIR in diagnosis.actions and state.repairs_used >= state.max_repairs:
        return f"the repair limit of {state.max_repairs} is used up"
    return None


def _why_not(action: Action, diagnosis: Diagnosis, state: RecoveryState, config: RecoveryConfig) -> str | None:
    """None when the action is allowed now."""
    tests_only = diagnosis.source == VERIFICATION and action is Action.RETRY  # the code stays as it is, so no tool runs
    if not tests_only:
        if not state.tools.can_run:
            return f"no eligible tool can run: {state.tools.why}"
        if state.seconds_left is not None and state.seconds_needed > state.seconds_left:
            return f"the last attempt took {state.seconds_needed:.0f} seconds and the run has {state.seconds_left:.0f} left"
    if action is Action.SWITCH_TOOL:
        return None if state.tools.alternatives else "no other eligible tool exists"
    if state.streak >= config.repeat_limit:
        return f"the same failure came back {state.streak} times in a row"
    if action is Action.RECONFIGURE:
        return None if state.next_timeout is not None else "no longer time limit is approved"
    if action is Action.REPAIR and state.repairs_judged >= config.min_repair_samples:
        if reliability(state.repairs_verified, state.repairs_judged, config.alpha, config.beta) < config.min_repair_rate:
            return f"only {state.repairs_verified} of {state.repairs_judged} verified repairs of {diagnosis.category.value} passed"
    return None


def decide(diagnosis: Diagnosis, state: RecoveryState, config: RecoveryConfig | None = None) -> Decision:
    config = config or RecoveryConfig()
    used, allowed = (state.attempts_used, state.max_attempts) if diagnosis.source == CODING else (state.repairs_used, state.max_repairs)
    decision = Decision(
        diagnosis.failure_class, Action.STOP, "", used, allowed, category=diagnosis.category.value, confidence=diagnosis.confidence.value, evidence=diagnosis.evidence,
        recovery_safe=diagnosis.recovery_safe, actions=tuple(a.value for a in diagnosis.actions), location=diagnosis.location,
    )  # fmt: skip
    if not diagnosis.recovery_safe:
        return replace(decision, reason=_NOT_SAFE[diagnosis.category.value])
    limit = _limit_reached(diagnosis, state)
    if limit is not None:
        return replace(decision, reason=limit, hint=hint_for(diagnosis.failure_class))
    reasons: list[str] = []
    for action in diagnosis.actions:
        if action is Action.STOP:
            break
        why = _why_not(action, diagnosis, state, config)
        if why is None:
            return _taken(decision, action, diagnosis, state)
        reasons.append(why)
    return replace(decision, reason="; ".join(dict.fromkeys(reasons)) or "no recovery applies to this failure")


def _taken(decision: Decision, action: Action, diagnosis: Diagnosis, state: RecoveryState) -> Decision:
    if action is Action.SWITCH_TOOL:
        tool = state.tools.alternatives[0]
        kind = "another tool" if state.tools.real_alternative else "another simulated tool"
        return replace(decision, action=action, reason=f"{kind}, {tool}, is eligible and has not failed here", hint=_HINTS[action], tool=tool, real_switch=state.tools.real_alternative)
    if action is Action.RECONFIGURE:
        return replace(decision, action=action, reason=f"the attempt ran out of time, and {state.next_timeout:g} seconds is approved for this task", hint=_HINTS[action], timeout_seconds=state.next_timeout)
    if action is Action.REPAIR:
        return replace(decision, action=action, reason=f"{diagnosis.category.value}: the code can be corrected from this evidence, and a repair is still allowed", hint=hint_for(diagnosis.failure_class))
    if diagnosis.source == VERIFICATION:
        return replace(decision, action=action, reason="the sandbox gave no answer about the code, so the tests may run once more", hint=_TEST_AGAIN_HINT)
    return replace(decision, action=action, reason="the attempt was undone and proved so, and the task may run again", hint=_HINTS[action])
