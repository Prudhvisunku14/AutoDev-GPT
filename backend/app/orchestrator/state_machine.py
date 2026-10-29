"""Run state machine (ARCHITECTURE §4.1). Every state change goes through `transition`."""

from __future__ import annotations

from app.db.models import TERMINAL_RUN_STATES, Run, RunState

S = RunState

_FORWARD: dict[RunState, frozenset[RunState]] = {
    S.CREATED: frozenset({S.ANALYZING}),
    S.ANALYZING: frozenset({S.PLANNING}),
    S.PLANNING: frozenset({S.DEFINING_CRITERIA}),
    S.DEFINING_CRITERIA: frozenset({S.IMPLEMENTING}),
    S.IMPLEMENTING: frozenset({S.VERIFYING}),
    S.VERIFYING: frozenset({S.DOCUMENTING, S.REPAIRING}),
    S.REPAIRING: frozenset({S.VERIFYING}),
    S.DOCUMENTING: frozenset({S.COMPLETED}),
}
_ENDINGS = frozenset({S.FAILED, S.CANCELLED, S.STOPPED_BY_POLICY})

# States the orchestrator can drive in this build. A run that reaches any other
# non-terminal state is paused until the phase that implements it exists.
ORCHESTRATED_STATES = frozenset({S.ANALYZING, S.PLANNING, S.DEFINING_CRITERIA})
REQUIRED_PHASE = {
    S.IMPLEMENTING: "Phase 3 (Docker sandbox) and Phase 4 (OpenCode)",
    S.VERIFYING: "Phase 5 (verification-guided development)",
    S.REPAIRING: "Phase 6 (failure classification and recovery)",
    S.DOCUMENTING: "a later phase (documentation and delivery)",
}


class InvalidTransition(Exception):
    pass


def allowed_targets(run: Run) -> frozenset[RunState]:
    if run.state in TERMINAL_RUN_STATES:
        return frozenset()
    if run.state is S.AWAITING_APPROVAL:
        if run.resume_state is None:
            return _ENDINGS
        return _ENDINGS | {run.resume_state}
    targets = _FORWARD.get(run.state, frozenset()) | _ENDINGS
    if run.state is not S.CREATED:
        targets |= {S.AWAITING_APPROVAL}
    return targets


def transition(run: Run, target: RunState) -> RunState:
    """Apply a transition or raise InvalidTransition. Returns the previous state."""
    previous = run.state
    if target not in allowed_targets(run):
        raise InvalidTransition(f"{previous} -> {target} is not allowed")
    if target is S.AWAITING_APPROVAL:
        run.resume_state = previous
    elif previous is S.AWAITING_APPROVAL or target in TERMINAL_RUN_STATES:
        run.resume_state = None
    run.state = target
    return previous
