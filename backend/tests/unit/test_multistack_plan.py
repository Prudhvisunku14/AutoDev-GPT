"""Multi-stack plans: components with their own stacks and folders, tasks per component, dependencies across them,
and the old one-stack plans unchanged. Through the real orchestrator and gates, with FakeLLM."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from app.db.models import PlanTask, RunState
from app.demo import mvp, weather
from app.orchestrator.fake_llm import FakeLLM, call
from tests.harness import Harness
from tests.unit.test_coding_tools import GOOD, TESTS, Rig, generate, pause


def plan_result(tmp_path: Path, plan: dict[str, Any], criteria: dict[str, Any] | None = None, analysis: dict[str, Any] = weather.ANALYSIS) -> tuple[Harness, str, dict[str, Any]]:
    steps = [call("submit_analysis", analysis), call("submit_plan", plan)] + ([call("submit_criteria", criteria)] if criteria else []) + [pause()]
    h = Harness(tmp_path, FakeLLM(*steps))
    run_id = h.new_run(weather.REQUIREMENT, "Weather")
    h.advance(run_id)
    outputs = [json.loads(i.output_json) for i in h.invocations(run_id) if i.output_json]
    return h, run_id, outputs[-1]


def changed(**edits: Any) -> dict[str, Any]:
    plan = copy.deepcopy(weather.FULLSTACK_PLAN)
    for key, value in edits.items():
        plan[key] = value(plan) if callable(value) else value
    return plan


def test_a_two_component_plan_is_accepted_and_keeps_components_and_dependencies(tmp_path: Path) -> None:
    h, run_id, result = plan_result(tmp_path, weather.FULLSTACK_PLAN)
    assert result["ok"] and h.run(run_id).state is RunState.DEFINING_CRITERIA
    with h.sessions() as s:
        rows = s.query(PlanTask).filter(PlanTask.run_id == run_id).order_by(PlanTask.order).all()
        assert [(r.component, json.loads(r.depends_on_json)) for r in rows] == [("backend", []), ("backend", [1]), ("frontend", [1]), ("frontend", [3])]
    assert [c["name"] for c in json.loads(h.run(run_id).plan_json)["components"]] == ["backend", "frontend"]


def _set_task(index: int, **values: Any):
    def edit(plan: dict[str, Any]) -> list[dict[str, Any]]:
        tasks = plan["tasks"]
        tasks[index] = {**tasks[index], **values}
        return tasks

    return edit


@pytest.mark.parametrize(
    ("plan", "problem"),
    [
        (changed(components=lambda p: [{**p["components"][0], "stack": "cobol-mainframe"}, p["components"][1]]), "component backend: unknown stack 'cobol-mainframe'"),
        (changed(components=lambda p: [p["components"][0], {**p["components"][1], "path": "backend"}]), "component folders must be unique"),
        (changed(stack="python-cli"), "stack 'python-cli' must be the stack of one of the components"),
        (changed(tasks=_set_task(2, component="mobile")), "task 3 must name one of the components: backend, frontend"),
        (changed(tasks=_set_task(2, features={**weather.FULLSTACK_PLAN["tasks"][2]["features"], "language": "python"})), "task 3 language 'python' does not match its stack's language 'typescript'"),
        (changed(tasks=_set_task(0, depends_on=[4])), "task dependencies form a cycle"),
        (changed(tasks=_set_task(1, depends_on=[2])), "task 2 cannot depend on task 2"),
        (changed(tasks=_set_task(1, depends_on=[9])), "task 2 cannot depend on task 9"),
        (changed(tasks=lambda p: [t for t in p["tasks"] if t["component"] == "backend"]), "components without any task: frontend"),
    ],
)
def test_an_invalid_or_inconsistent_plan_is_refused(tmp_path: Path, plan: dict[str, Any], problem: str) -> None:
    _h, _run, result = plan_result(tmp_path, plan)
    assert result["error"]["class"] == "VALIDATION_FAILED" and any(problem in d for d in result["error"]["details"]), result["error"]["details"]


def test_front_end_requirements_cannot_be_dropped(tmp_path: Path) -> None:
    plan = changed(tasks=lambda p: [p["tasks"][0], p["tasks"][1]], components=lambda p: p["components"][:1])
    _h, _run, result = plan_result(tmp_path, plan)
    assert "requirements not covered by any task: R-001, R-002, R-003" in result["error"]["details"]


def test_a_component_on_a_one_stack_plan_is_refused(tmp_path: Path) -> None:
    plan = copy.deepcopy(weather.PLAN)
    plan["tasks"][0]["component"] = "backend"
    _h, _run, result = plan_result(tmp_path, plan)
    assert any("names component 'backend', but the plan has no components" in d for d in result["error"]["details"])


def test_a_one_stack_plan_written_before_components_existed_is_still_accepted(tmp_path: Path) -> None:
    h = Harness(tmp_path, FakeLLM(*mvp._calculator_opening(), pause()))
    run_id = h.new_run()
    h.advance(run_id)
    assert h.run(run_id).state is RunState.IMPLEMENTING and "components" not in mvp._CALCULATOR_PLAN["tasks"][0]


def test_each_components_stack_brings_its_own_criteria_methods(tmp_path: Path) -> None:
    h, run_id, result = plan_result(tmp_path, weather.FULLSTACK_PLAN, weather.FULL_CRITERIA)
    assert result["ok"] and h.run(run_id).state is RunState.IMPLEMENTING
    e2e = {"criteria": [{**c, "method": "e2e" if c["code"] == "AC-001" else c["method"]} for c in weather.CRITERIA["criteria"]]}
    _h, _run, single = plan_result(tmp_path / "single", weather.BACKEND_PLAN, e2e, weather.BACKEND_ANALYSIS)
    assert any("AC-001 uses method 'e2e', which the plan's stack python-fastapi does not support" in d for d in single["error"]["details"])


def test_a_task_waits_for_the_tasks_it_depends_on(tmp_path: Path) -> None:
    plan = copy.deepcopy(mvp._CALCULATOR_PLAN)
    plan["tasks"][1]["depends_on"] = [1]
    opening = [call("submit_analysis", mvp._CALCULATOR_ANALYSIS), call("submit_plan", plan), call("submit_criteria", mvp._CALCULATOR_CRITERIA)]
    r = Rig(tmp_path, *opening, generate(2), generate(1), generate(2), pause(), coding=[GOOD, TESTS])
    r.advance()
    first, *_rest = [json.loads(i.output_json) for i in r.h.invocations(r.run_id) if i.tool == "generate_code"]
    assert first["error"]["summary"] == "task 2 depends on tasks that are not done yet: 1"
    assert [o["ok"] for o in [json.loads(i.output_json) for i in r.h.invocations(r.run_id) if i.tool == "generate_code"]] == [False, True, True]
