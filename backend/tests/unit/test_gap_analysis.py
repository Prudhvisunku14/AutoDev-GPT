"""Phase 3: GPT's comparison of existing code with the requirements, checked in code before any plan is accepted."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.coding.brief import BriefInput, build_brief
from app.db.models import RunState
from app.demo import mvp
from app.orchestrator.fake_llm import call
from app.tools.adapters.gap import SubmitGapAnalysisArgs, brief_text, problems, register_gap_tool
from app.tools.registry import strict_json_schema
from tests.harness import Harness
from tests.unit.test_coding_tools import pause

REQUIREMENTS = {"R-001": ("functional", "Add, subtract, multiply and divide two numbers."), "R-002": ("functional", "Dividing by zero raises a clear error."),
                "R-003": ("non_functional", "The project includes automated unit tests.")}  # fmt: skip
FILES = {"calculator.py", "README.md", "LICENSE", "docs/usage.md", "tests/test_old.py"}


def gap(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "existing_functionality": ["calculator.py adds and subtracts two numbers"],
        "similarities": [{"requirement_ref": "R-001", "existing": "calculator.py: add and subtract"}],
        "missing_features": [{"requirement_ref": "R-002", "description": "divide must raise ZeroDivisionError with a clear message"}],
        "conflicts": [{"path": "calculator.py", "description": "divide returns None for zero", "resolution": "raise instead"}],
        "architecture_changes": ["keep one module; add a tests file"],
        "files": [{"path": "calculator.py", "change": "modify", "reason": "add multiply and divide"},
                  {"path": "test_calculator.py", "change": "add", "reason": "unit tests for every criterion"},
                  {"path": "LICENSE", "change": "keep", "reason": "reused code keeps its licence"}],
        "tests": [{"requirement_ref": "R-002", "description": "dividing by zero raises"}],
        "reuse_decisions": [{"component": "calculator.py", "decision": "adapt", "reason": "it already has two of the four operations"}],
    }  # fmt: skip
    return base | overrides


def check(args: dict[str, Any], files: set[str] | None = FILES) -> list[str]:
    return problems(SubmitGapAnalysisArgs.model_validate(args), REQUIREMENTS, files, {"LICENSE"})


def test_a_complete_analysis_has_no_problems() -> None:
    assert check(gap()) == []


def test_the_schema_is_strict() -> None:
    schema = strict_json_schema(SubmitGapAnalysisArgs)
    assert schema["additionalProperties"] is False and set(schema["required"]) == set(SubmitGapAnalysisArgs.model_fields)


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"similarities": []}, "R-001 (Add, subtract, multiply and divide two numbers.) is neither met by the existing code nor listed as missing"),
        ({"missing_features": [{"requirement_ref": "R-009", "description": "x"}]}, "R-009 is not a requirement of this run"),
        ({"tests": [{"requirement_ref": "R-001", "description": "x"}]}, "missing feature R-002 has no planned test"),
        ({"files": [{"path": "nope.py", "change": "modify", "reason": "r"}]}, "nope.py does not exist in the existing code, so it cannot be modifyd"),
        ({"files": [{"path": "README.md", "change": "add", "reason": "r"}]}, "README.md already exists: modify it instead of adding it"),
        ({"files": [{"path": "LICENSE", "change": "delete", "reason": "r"}]}, "LICENSE is a licence or notice file: reused code keeps its licence and attribution"),
        ({"files": [{"path": "../outside.py", "change": "add", "reason": "r"}]}, "../outside.py: a path inside the project, written with /"),
        ({"reuse_decisions": [{"component": "calculator.py", "decision": "replace", "reason": "nicer"}]}, "replacing calculator.py needs a reason of at least a sentence"),
    ],
)
def test_each_rule_is_enforced_in_code(change: dict[str, Any], expected: str) -> None:
    assert expected in check(gap(**change))


def test_a_rewrite_that_deletes_most_files_is_refused() -> None:
    deletions = [{"path": p, "change": "delete", "reason": "start over"} for p in ("README.md", "docs/usage.md", "tests/test_old.py")]
    found = check(gap(files=deletions))
    assert any("DevPilot adapts existing code rather than overwriting it" in p for p in found)


def test_with_no_file_list_paths_are_not_checked_but_licences_still_are() -> None:
    assert check(gap(files=[{"path": "anything.py", "change": "modify", "reason": "r"}]), files=None) == []
    assert any("licence" in p for p in check(gap(files=[{"path": "COPYING.txt", "change": "delete", "reason": "r"}]), files=None))


def test_the_coding_brief_carries_the_accepted_analysis() -> None:
    text = brief_text(SubmitGapAnalysisArgs.model_validate(gap()).model_dump())
    assert "do not rewrite it" in text and "- modify calculator.py: add multiply and divide" in text and "- adapt calculator.py" in text
    brief = build_brief(BriefInput(1, 2, "t", "d", "code_generation", [], [], "python-cli", "Python", "go", ["calculator.py"], gap=text))
    assert "the accepted gap analysis" in brief and "add multiply and divide" in brief
    assert "gap analysis" not in build_brief(BriefInput(1, 2, "t", "d", "code_generation", [], [], "python-cli", "Python", "go", []))


# --- In a run ----------------------------------------------------------------------------------------------------


def started_from_existing_code(h: Harness, run_id: str) -> None:
    """What a resource check records when the requirement names a repository DevPilot read (sources/check.py)."""
    with h.sessions.begin() as s:
        h.events.record(s, project_id=h.run(run_id).project_id, type="resources.checked",
                        payload={"resources": [{"kind": "github", "status": "accessible", "full_name": "acme/calc", "paths": sorted(FILES)}], "problems": [], "needs_permission": []})  # fmt: skip


def gap_harness(tmp_path: Path, *steps: Any) -> tuple[Harness, str]:
    h = Harness(tmp_path, None)
    register_gap_tool(h.registry)
    h.llm.extend(*steps)
    run_id = h.new_run()
    started_from_existing_code(h, run_id)
    return h, run_id


def test_a_plan_for_existing_code_waits_for_an_accepted_gap_analysis(tmp_path: Path) -> None:
    analysis, plan, _criteria = mvp._calculator_opening()
    h, run_id = gap_harness(tmp_path, analysis, plan, call("submit_gap_analysis", gap()), plan, pause())
    run = h.advance(run_id)
    outputs = [o for o in h.outputs(run_id) if o["tool"] in ("submit_plan", "submit_gap_analysis")]
    assert outputs[0]["tool"] == "submit_plan" and outputs[0]["ok"] is False and "call submit_gap_analysis first" in outputs[0]["error"]["summary"]
    assert outputs[1] == {**outputs[1], "ok": True} and outputs[1]["data"]["recorded"] is True
    assert outputs[2]["tool"] == "submit_plan" and outputs[2]["ok"] is True
    assert run.state is RunState.DEFINING_CRITERIA
    assert "analysis.gap_recorded" in h.event_types(run_id)


def test_a_rejected_analysis_returns_every_problem_and_records_nothing(tmp_path: Path) -> None:
    analysis, _plan, _criteria = mvp._calculator_opening()
    bad = gap(similarities=[], files=[{"path": "LICENSE", "change": "delete", "reason": "r"}])
    h, run_id = gap_harness(tmp_path, analysis, call("submit_gap_analysis", bad), pause())
    h.advance(run_id)
    out = next(o for o in h.outputs(run_id) if o["tool"] == "submit_gap_analysis")
    assert out["ok"] is False and out["error"]["class"] == "VALIDATION_FAILED" and len(out["error"]["details"]) == 2
    assert "analysis.gap_recorded" not in h.event_types(run_id)


def test_a_new_project_plans_without_a_gap_analysis(tmp_path: Path) -> None:
    analysis, plan, _criteria = mvp._calculator_opening()
    h = Harness(tmp_path, None)
    register_gap_tool(h.registry)
    h.llm.extend(analysis, plan, pause())
    run_id = h.new_run()
    assert h.advance(run_id).state is RunState.DEFINING_CRITERIA
