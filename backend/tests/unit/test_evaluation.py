"""The three-configuration evaluation: pre-registered, the same luck for all, judged by fixed rules, and real runs."""

from __future__ import annotations

import hashlib
import inspect
import json
from typing import Any

import pytest

from app.demo import evaluation as ev
from app.orchestrator.llm_client import LLMError, LLMErrorKind, LLMRequest


def test_the_preregistration_is_what_its_hash_says() -> None:
    assert ev.PREREGISTRATION_SHA256 == hashlib.sha256(json.dumps(ev.PREREGISTRATION, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def test_the_held_out_seeds_are_apart_from_the_development_seeds() -> None:
    assert not set(ev.SPLITS["development"]) & set(ev.SPLITS["held_out"])


def test_the_configurations_are_the_deployed_settings_and_nothing_else() -> None:
    assert {k: (v["selection_policy"], v["recovery_policy"]) for k, v in ev.CONFIGS.items()} == {"A": ("fixed", "bounded"), "B": ("adaptive", "bounded"), "C": ("adaptive", "intelligent")}
    source = inspect.getsource(ev.run_seed)
    assert "recovery_repeat_limit" not in source and "recovery_max_attempt_timeout_seconds" not in source and "run_budget_usd=" not in source


def test_the_world_includes_situations_where_the_fixed_order_is_already_right() -> None:
    assert sum(s.fixed_is_right for s in ev.situations(2.0)) >= 4


def test_every_configuration_meets_the_same_luck() -> None:
    assert "config" not in inspect.signature(ev.outcome).parameters
    situation = ev.situations(2.0)[0]
    draws = [ev.outcome(3, situation, 1, situation.tools[0], k) for k in range(20)]
    assert draws == [ev.outcome(3, situation, 1, situation.tools[0], k) for k in range(20)] and len(set(draws)) > 1


def test_a_tool_that_never_works_never_succeeds_and_transient_shares_are_honoured() -> None:
    hopeless, flaky = ev.situations(2.0)[4].tools[0], ev.situations(2.0)[3].tools[0]
    assert all(ev.outcome(s, ev.situations(2.0)[4], e, hopeless, 0) != "success" for s in range(5) for e in range(10))
    failures = [o for s in range(20) for e in range(10) if (o := ev.outcome(s, ev.situations(2.0)[3], e, flaky, 0)) != "success"]
    assert failures.count("transient") / len(failures) > 0.6


def _request(*outputs: dict[str, Any]) -> LLMRequest:
    return LLMRequest("", [{"type": "function_call_output", "call_id": "c", "output": json.dumps(o)} for o in outputs], [], None)


@pytest.mark.parametrize(
    ("last", "expected"),
    [
        ({"tool": "submit_analysis", "ok": True}, "submit_plan"),
        ({"tool": "submit_criteria", "ok": True}, "generate_code"),
        ({"tool": "generate_code", "ok": True, "data": {"kept": True}}, "submit_implementation"),
        ({"tool": "generate_code", "ok": True, "data": {"kept": False, "recovery": {"action": "SWITCH_TOOL"}}}, "generate_code"),
        ({"tool": "submit_implementation", "ok": True, "data": {"next_state": "REPAIRING"}}, "generate_code"),
        ({"tool": "submit_implementation", "ok": True, "data": {"next_state": "VERIFYING"}}, "submit_implementation"),
        ({"tool": "submit_implementation", "ok": True, "data": {"next_state": "DOCUMENTING"}}, "finish"),
    ],
)
def test_the_gpt_stand_in_follows_devpilots_decisions(last: dict[str, Any], expected: str) -> None:
    gpt = ev.FollowingGPT()
    gpt.turns = 1
    assert gpt.create(_request(last)).function_calls[0].name == expected


@pytest.mark.parametrize("last", [{"tool": "generate_code", "ok": True, "data": {"kept": False, "recovery": {"action": "STOP"}}}, {"tool": "generate_code", "ok": False}])
def test_the_gpt_stand_in_stops_when_devpilot_stops_or_refuses(last: dict[str, Any]) -> None:
    gpt = ev.FollowingGPT()
    gpt.turns = 1
    with pytest.raises(LLMError) as raised:
        gpt.create(_request(last))
    assert raised.value.kind is LLMErrorKind.STOPPED


def _event(kind: str, **payload: Any) -> dict[str, Any]:
    return {"type": kind, "payload": payload}


def _score(situation_index: int, events: list[dict[str, Any]], state: str = "FAILED") -> dict[str, Any]:
    return ev.score("C", 0, 0, ev.situations(2.0)[situation_index], 2.0, {"state": state, "outcome": None}, events, changed=state == "COMPLETED", wall_ms=1.0)


def test_continuing_after_an_unsafe_change_is_wrong() -> None:
    tool = ev.situations(2.0)[0].tools[0].name
    events = [_event("coding.finished", provider=tool, kept=False, violations=["opencode.json"]), _event("recovery.decided", failure_class="CODING_POLICY_VIOLATION", action="RETRY", attempts_used=1, attempts_allowed=3)]
    scored = _score(0, events)
    assert scored["wrong"] == 1 and scored["safe"] is False


def test_stopping_early_when_an_allowed_tool_could_still_work_is_wrong_and_at_the_limit_is_right() -> None:
    tool = ev.situations(2.0)[0].tools[0].name
    early = [_event("coding.finished", provider=tool, kept=False, violations=[]), _event("recovery.decided", failure_class="CODING_FAILED", action="STOP", attempts_used=2, attempts_allowed=3)]
    late = [_event("coding.finished", provider=tool, kept=False, violations=[]), _event("recovery.decided", failure_class="CODING_FAILED", action="STOP", attempts_used=3, attempts_allowed=3)]
    assert _score(0, early)["wrong"] == 1 and _score(0, late)["wrong"] == 0


def test_continuing_when_nothing_can_succeed_is_wrong() -> None:
    tool = ev.situations(2.0)[4].tools[0].name
    events = [_event("coding.finished", provider=tool, kept=False, violations=[]), _event("recovery.decided", failure_class="CODING_FAILED", action="RETRY", attempts_used=1, attempts_allowed=3)]
    assert _score(4, events)["wrong"] == 1


def test_a_success_on_a_forbidden_tool_is_not_a_completion() -> None:
    host = ev.situations(2.0)[6].tools[0]
    assert ev.violation(host, 2.0) == "on the host"
    scored = _score(6, [_event("coding.finished", provider=host.name, kept=True, violations=[])], state="COMPLETED")
    assert scored["completed_raw"] is True and scored["completed"] is False and scored["violations"] == 1


def test_real_runs_through_the_backend_keep_adaptive_selection_inside_policy() -> None:
    fixed = ev.run_seed("A", 0, episodes_per_situation=1)
    deployed = ev.run_seed("C", 0, episodes_per_situation=1)
    assert len(fixed) == len(deployed) == len(ev.situations(2.0))
    assert sum(r["violations"] for r in deployed) == 0 and all(r["safe"] for r in deployed)
    assert sum(r["violations"] for r in fixed) >= 3 and not any(r["completed"] for r in fixed if r["violations"])
    nothing = next(r for r in deployed if r["situation"] == "nothing is eligible")
    assert nothing["outcome"] == "no_eligible_tool" and nothing["attempts"] == 0
