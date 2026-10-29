"""State machine transitions, PolicyEngine decisions and RunGuard limits."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.db.models import TERMINAL_RUN_STATES, Run, RunState
from app.orchestrator.budget import RunGuard, turn_cost_usd
from app.orchestrator.llm_client import Usage
from app.orchestrator.state_machine import InvalidTransition, allowed_targets, transition
from app.tools.base import RiskLevel, ToolArgs, ToolSpec
from app.tools.policy import Decision, PolicyEngine
from tests.helpers import make_settings

S = RunState


def _run(state: RunState, resume: RunState | None = None) -> Run:
    return Run(project_id="p" * 32, state=state, resume_state=resume)


@pytest.mark.parametrize(
    ("start", "target"),
    [
        (S.CREATED, S.ANALYZING),
        (S.ANALYZING, S.PLANNING),
        (S.PLANNING, S.DEFINING_CRITERIA),
        (S.DEFINING_CRITERIA, S.IMPLEMENTING),
        (S.IMPLEMENTING, S.VERIFYING),
        (S.VERIFYING, S.REPAIRING),
        (S.VERIFYING, S.DOCUMENTING),
        (S.REPAIRING, S.VERIFYING),
        (S.DOCUMENTING, S.COMPLETED),
        (S.PLANNING, S.AWAITING_APPROVAL),
        (S.ANALYZING, S.STOPPED_BY_POLICY),
        (S.CREATED, S.CANCELLED),
        (S.DEFINING_CRITERIA, S.FAILED),
    ],
)
def test_allowed_transitions(start: RunState, target: RunState) -> None:
    run = _run(start)
    assert transition(run, target) is start
    assert run.state is target


@pytest.mark.parametrize(
    ("start", "target"),
    [
        (S.CREATED, S.PLANNING),  # cannot skip analysis
        (S.ANALYZING, S.DEFINING_CRITERIA),
        (S.PLANNING, S.ANALYZING),  # no going back
        (S.DEFINING_CRITERIA, S.COMPLETED),
        (S.IMPLEMENTING, S.COMPLETED),
        (S.CREATED, S.AWAITING_APPROVAL),
        (S.COMPLETED, S.ANALYZING),
        (S.CANCELLED, S.FAILED),
    ],
)
def test_forbidden_transitions(start: RunState, target: RunState) -> None:
    run = _run(start)
    with pytest.raises(InvalidTransition):
        transition(run, target)
    assert run.state is start


def test_awaiting_approval_resumes_only_the_previous_state() -> None:
    run = _run(S.PLANNING)
    transition(run, S.AWAITING_APPROVAL)
    assert run.resume_state is S.PLANNING
    assert allowed_targets(run) == {S.PLANNING, S.FAILED, S.CANCELLED, S.STOPPED_BY_POLICY}
    with pytest.raises(InvalidTransition):
        transition(run, S.DEFINING_CRITERIA)
    transition(run, S.PLANNING)
    assert run.resume_state is None


def test_ending_clears_resume_state() -> None:
    run = _run(S.AWAITING_APPROVAL, resume=S.ANALYZING)
    transition(run, S.CANCELLED)
    assert run.resume_state is None


@pytest.mark.parametrize("state", sorted(TERMINAL_RUN_STATES))
def test_terminal_states_have_no_exits(state: RunState) -> None:
    assert allowed_targets(_run(state)) == frozenset()


# --- PolicyEngine ---------------------------------------------------------------


class _Args(ToolArgs):
    x: int


def _spec(risk: RiskLevel, states: frozenset[RunState] | None = None, asks_human: bool = False) -> ToolSpec:
    return ToolSpec(
        name="t", description="d", args_model=_Args, risk=risk, states=states,
        handler=None if asks_human else (lambda c, a: {}), asks_human=asks_human,
    )


@pytest.mark.parametrize("risk", [RiskLevel.READ, RiskLevel.WORKSPACE_WRITE, RiskLevel.SANDBOX_EXEC])
def test_low_risk_is_allowed(risk: RiskLevel) -> None:
    assert PolicyEngine().decide(_spec(risk), S.ANALYZING).decision is Decision.ALLOW


@pytest.mark.parametrize("risk", [RiskLevel.EXTERNAL_NETWORK, RiskLevel.GIT_PUSH, RiskLevel.DESTRUCTIVE])
def test_risky_actions_need_approval_until_approved(risk: RiskLevel) -> None:
    policy = PolicyEngine()
    assert policy.decide(_spec(risk), S.ANALYZING).decision is Decision.REQUIRE_APPROVAL
    assert policy.decide(_spec(risk), S.ANALYZING, approved=True).decision is Decision.ALLOW


def test_wrong_state_is_denied_even_if_approved() -> None:
    spec = _spec(RiskLevel.GIT_PUSH, states=frozenset({S.DOCUMENTING}))
    decision = PolicyEngine().decide(spec, S.ANALYZING, approved=True)
    assert decision.decision is Decision.DENY
    assert "not available in state ANALYZING" in decision.reason


@pytest.mark.parametrize("state", [S.CREATED, S.AWAITING_APPROVAL, S.COMPLETED, S.CANCELLED])
def test_no_tool_runs_outside_working_states(state: RunState) -> None:
    assert PolicyEngine().decide(_spec(RiskLevel.READ), state).decision is Decision.DENY


def test_human_input_tools_ask_a_person() -> None:
    assert PolicyEngine().decide(_spec(RiskLevel.READ, asks_human=True), S.PLANNING).decision is Decision.ASK_HUMAN


# --- Budget ---------------------------------------------------------------------


def test_turn_cost_math(tmp_path: Path) -> None:
    s = make_settings(tmp_path, openai_price_input_per_mtok=2.5, openai_price_output_per_mtok=10.0)
    assert turn_cost_usd(Usage(input_tokens=1_000_000, output_tokens=100_000), s) == pytest.approx(3.5)
    assert turn_cost_usd(Usage(input_tokens=400, output_tokens=0), s) == pytest.approx(0.001)
    assert turn_cost_usd(Usage(1, 1), make_settings(tmp_path)) is None  # prices unknown
    assert turn_cost_usd(Usage(1, 1), make_settings(tmp_path, openai_price_input_per_mtok=1.0)) is None


def test_empty_price_strings_mean_unknown(tmp_path: Path) -> None:
    s = make_settings(tmp_path, openai_price_input_per_mtok="", openai_price_output_per_mtok="")
    assert not s.cost_known


@pytest.mark.parametrize(
    ("fields", "active", "reason"),
    [
        ({"turns": 5}, 0, "turn_limit"),
        ({"tokens_in": 900, "tokens_out": 100}, 0, "token_limit"),
        ({"cost_usd": 1.0, "cost_known": True}, 0, "budget_limit"),
        ({}, 61, "wall_clock_limit"),
    ],
)
def test_guard_before_llm(tmp_path: Path, fields: dict[str, object], active: float, reason: str) -> None:
    guard = RunGuard(make_settings(tmp_path, max_turns=5, run_max_tokens=1000, run_budget_usd=1.0, run_wall_clock_seconds=60))
    run = Run(turns=0, tokens_in=0, tokens_out=0, cost_usd=0.0, cost_known=True, tool_calls=0)
    for k, v in fields.items():
        setattr(run, k, v)
    hit = guard.before_llm(run, active)
    assert hit is not None and hit.reason == reason


def test_guard_ignores_cost_when_unknown_and_passes_under_limits(tmp_path: Path) -> None:
    guard = RunGuard(make_settings(tmp_path, run_budget_usd=1.0))
    run = Run(turns=0, tokens_in=10, tokens_out=10, cost_usd=5.0, cost_known=False, tool_calls=0)
    assert guard.before_llm(run, 0) is None


def test_guard_before_tool(tmp_path: Path) -> None:
    guard = RunGuard(make_settings(tmp_path, max_tool_calls=2, run_wall_clock_seconds=10))
    assert guard.before_tool(Run(tool_calls=1), 0) is None
    assert guard.before_tool(Run(tool_calls=2), 0).reason == "tool_call_limit"  # type: ignore[union-attr]
    assert guard.before_tool(Run(tool_calls=0), 10).reason == "wall_clock_limit"  # type: ignore[union-attr]
