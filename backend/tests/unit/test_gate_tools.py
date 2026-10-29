"""Code-level validation of the gate tools. Rejected submissions leave no rows behind."""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from app.db.models import AcceptanceCriterion, PlanTask, Project, Requirement, Run, RunState
from app.demo.calculator import ANALYSIS, CRITERIA, PLAN
from app.tools.base import ErrorClass, ToolContext, ToolResult
from app.tools.executor import PreparedCall
from tests.harness import Harness


@pytest.fixture
def h(tmp_path: Path) -> Iterator[Harness]:
    harness = Harness(tmp_path)
    yield harness
    harness.close()


def _call(h: Harness, run_id: str, tool: str, args: dict[str, Any]) -> ToolResult:
    with h.sessions.begin() as s:
        run = s.get(Run, run_id)
        project = s.get(Project, run.project_id)  # type: ignore[union-attr]
        prepared = h.executor.prepare(run.state, tool, json.dumps(args))  # type: ignore[union-attr]
        if isinstance(prepared, ToolResult):
            return prepared
        assert isinstance(prepared, PreparedCall)
        ctx = ToolContext(session=s, run=run, project=project, settings=h.settings, events=h.events, catalog=h.catalog)  # type: ignore[arg-type]
        return h.executor.invoke(ctx, prepared)


def _run_in(h: Harness, state: RunState) -> str:
    """A run already advanced to `state` through the real gates."""
    run_id = h.new_run()
    with h.sessions.begin() as s:
        s.get(Run, run_id).state = RunState.ANALYZING  # type: ignore[union-attr]
    steps = [("submit_analysis", ANALYSIS), ("submit_plan", PLAN)]
    order = [RunState.ANALYZING, RunState.PLANNING, RunState.DEFINING_CRITERIA]
    for tool, args in steps[: order.index(state)]:
        assert _call(h, run_id, tool, args).ok
    assert h.run(run_id).state is state
    return run_id


def _count(h: Harness, model: type, run_id: str) -> int:
    with h.sessions() as s:
        return s.scalar(select(func.count()).select_from(model).where(model.run_id == run_id)) or 0  # type: ignore[attr-defined]


def _rejected(result: ToolResult, *fragments: str) -> None:
    assert not result.ok
    assert result.error is not None and result.error.error_class == ErrorClass.VALIDATION_FAILED
    details = "\n".join(result.error.details or [])
    for fragment in fragments:
        assert fragment in details, details


# --- submit_analysis --------------------------------------------------------------


def test_analysis_is_persisted_and_advances(h: Harness) -> None:
    run_id = _run_in(h, RunState.ANALYZING)
    result = _call(h, run_id, "submit_analysis", ANALYSIS)
    assert result.ok and result.data["next_state"] == "PLANNING"  # type: ignore[index]
    assert result.state == "PLANNING"
    assert _count(h, Requirement, run_id) == 5
    assert json.loads(h.run(run_id).analysis_json)["assumptions"] == ANALYSIS["assumptions"]  # type: ignore[arg-type]


def test_analysis_codes_must_be_sequential_and_texts_unique(h: Harness) -> None:
    run_id = _run_in(h, RunState.ANALYZING)
    bad = copy.deepcopy(ANALYSIS)
    bad["requirements"][1]["code"] = "R-007"
    bad["requirements"][2]["text"] = bad["requirements"][0]["text"].upper()
    _rejected(_call(h, run_id, "submit_analysis", bad), "in order", "R-003 repeats R-001")
    assert _count(h, Requirement, run_id) == 0
    assert h.run(run_id).state is RunState.ANALYZING


def test_analysis_schema_errors_are_invalid_args(h: Harness) -> None:
    run_id = _run_in(h, RunState.ANALYZING)
    bad = copy.deepcopy(ANALYSIS)
    bad["requirements"][0]["code"] = "REQ-1"
    bad["requirements"][1]["kind"] = "nice_to_have"
    bad["extra"] = True
    result = _call(h, run_id, "submit_analysis", bad)
    assert result.error is not None and result.error.error_class == ErrorClass.INVALID_ARGS
    details = "\n".join(result.error.details or [])
    assert "requirements.0.code" in details and "requirements.1.kind" in details and "extra" in details


# --- submit_plan ------------------------------------------------------------------


def test_plan_is_persisted_sets_stack_and_advances(h: Harness) -> None:
    run_id = _run_in(h, RunState.PLANNING)
    result = _call(h, run_id, "submit_plan", PLAN)
    assert result.ok
    assert result.data["allowed_methods"] == ["unit", "static", "manual"]  # type: ignore[index]
    assert h.project(run_id).stack == "python-cli"
    assert _count(h, PlanTask, run_id) == 3
    assert h.run(run_id).state is RunState.DEFINING_CRITERIA


@pytest.mark.parametrize(
    ("mutate", "fragments"),
    [
        (lambda p: p.update(stack="django"), ["unknown stack 'django'", "python-cli"]),
        (lambda p: p["tasks"][0].update(requirement_refs=["R-001", "R-099"]), ["task 1 references unknown requirements: R-099"]),
        (lambda p: p["tasks"].__setitem__(2, {**p["tasks"][2], "requirement_refs": ["R-001"]}), ["not covered by any task: R-005"]),
        (lambda p: p["tasks"][1].update(title=p["tasks"][0]["title"]), ["task 2 has the same title as task 1"]),
        (lambda p: p["tasks"][0]["features"].update(language="javascript"), ["task 1 language 'javascript'"]),
        (lambda p: [t.update(capability="documentation") for t in p["tasks"]], ["capability code_generation"]),
    ],
)
def test_plan_rejections(h: Harness, mutate: Any, fragments: list[str]) -> None:
    run_id = _run_in(h, RunState.PLANNING)
    bad = copy.deepcopy(PLAN)
    mutate(bad)
    _rejected(_call(h, run_id, "submit_plan", bad), *fragments)
    assert _count(h, PlanTask, run_id) == 0
    assert h.project(run_id).stack is None
    assert h.run(run_id).state is RunState.PLANNING


def test_plan_reports_every_problem_at_once(h: Harness) -> None:
    run_id = _run_in(h, RunState.PLANNING)
    bad = copy.deepcopy(PLAN)
    bad["stack"] = "nope"
    bad["tasks"][0]["requirement_refs"] = ["R-042"]
    _rejected(_call(h, run_id, "submit_plan", bad), "unknown stack 'nope'", "task 1 references unknown requirements: R-042")


# --- submit_criteria ----------------------------------------------------------------


def test_criteria_are_persisted_and_advance_to_implementing(h: Harness) -> None:
    run_id = _run_in(h, RunState.DEFINING_CRITERIA)
    result = _call(h, run_id, "submit_criteria", CRITERIA)
    assert result.ok and result.data == {"accepted": True, "criteria": 5, "must": 4, "next_state": "IMPLEMENTING"}
    assert _count(h, AcceptanceCriterion, run_id) == 5
    assert h.run(run_id).state is RunState.IMPLEMENTING


@pytest.mark.parametrize(
    ("mutate", "fragments"),
    [
        (lambda c: c["criteria"][0].update(method="manual"), ["AC-001 is MUST but uses the manual method"]),
        (lambda c: c["criteria"][2].update(method="api"), ["AC-003 uses method 'api'", "python-cli"]),
        (lambda c: c["criteria"][3].update(method="e2e"), ["AC-004 uses method 'e2e'"]),
        (lambda c: c["criteria"][1].update(code="AC-009"), ["criterion codes must be"]),
        (lambda c: c["criteria"][0].update(requirement_refs=["R-404"]), ["AC-001 references unknown requirements: R-404"]),
        (lambda c: c["criteria"].pop(4), ["requirements without any criterion: R-005"]),
        (lambda c: [x.update(priority="SHOULD") for x in c["criteria"]], ["at least one criterion must be MUST"]),
    ],
)
def test_criteria_rejections(h: Harness, mutate: Any, fragments: list[str]) -> None:
    run_id = _run_in(h, RunState.DEFINING_CRITERIA)
    bad = copy.deepcopy(CRITERIA)
    mutate(bad)
    _rejected(_call(h, run_id, "submit_criteria", bad), *fragments)
    assert _count(h, AcceptanceCriterion, run_id) == 0
    assert h.run(run_id).state is RunState.DEFINING_CRITERIA


def test_manual_is_allowed_for_should_criteria(h: Harness) -> None:
    run_id = _run_in(h, RunState.DEFINING_CRITERIA)
    ok = copy.deepcopy(CRITERIA)
    ok["criteria"][4]["method"] = "manual"
    assert _call(h, run_id, "submit_criteria", ok).ok


def test_gates_are_refused_in_other_states(h: Harness) -> None:
    run_id = _run_in(h, RunState.ANALYZING)
    result = _call(h, run_id, "submit_criteria", CRITERIA)
    assert result.error is not None and result.error.error_class == ErrorClass.POLICY_REFUSED
    assert "not available in state ANALYZING" in result.error.summary


def test_unexpected_handler_error_is_contained(h: Harness) -> None:
    run_id = _run_in(h, RunState.ANALYZING)
    spec = h.registry.get("submit_analysis")
    assert spec is not None

    def boom(ctx: ToolContext, args: Any) -> dict[str, object]:
        ctx.session.add(Requirement(run_id=run_id, code="R-001", text="partial", kind="functional"))
        ctx.session.flush()
        raise RuntimeError("bug")

    object.__setattr__(spec, "handler", boom)  # this harness's registry only
    result = _call(h, run_id, "submit_analysis", ANALYSIS)
    assert result.error is not None and result.error.error_class == ErrorClass.TOOL_ERROR
    assert "bug" not in result.error.summary  # internals are not leaked to GPT
    assert _count(h, Requirement, run_id) == 0  # savepoint rolled back the partial write


def test_render_truncates_large_results_to_valid_json(h: Harness) -> None:
    result = ToolResult(ok=True, tool="t", data={"blob": "x" * 50_000})
    text = h.executor.render(result)
    assert len(text) <= h.settings.tool_output_max_chars
    assert json.loads(text)["data"]["truncated"] is True

    huge_error = ToolResult.failure("t", ErrorClass.VALIDATION_FAILED, "s" * 30_000, ["d" * 10_000] * 10)
    text = h.executor.render(huge_error)
    assert len(text) <= h.settings.tool_output_max_chars
    assert json.loads(text)["error"]["class"] == "VALIDATION_FAILED"


def test_render_redacts_secrets(h: Harness) -> None:
    from tests.helpers import FAKE_OPENAI_KEY

    text = h.executor.render(ToolResult(ok=True, tool="t", data={"note": f"key {FAKE_OPENAI_KEY}"}))
    assert FAKE_OPENAI_KEY not in text
