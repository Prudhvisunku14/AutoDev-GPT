"""Independent acceptance through the real orchestrator, tools, database and guard. GPT and the coding provider are
scripted, and test execution is replayed from recordings: none of this is real Docker execution."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.coding.audit import take_snapshot
from app.coding.simulated import ScriptedAttempt, SimulatedLauncher
from app.db.models import Run, RunState, ToolOutcome
from app.demo import competition
from app.demo.fixtures import ACCEPTANCE_SUITE, LENIENT_CALCULATOR, LENIENT_TESTS, SEED
from app.sandbox.fake import FakeSandbox
from app.sandbox.replay import ReplaySandbox
from app.sandbox.spec import ExecResult, ExecSpec
from app.verification import acceptance
from tests.unit.test_coding_tools import GOOD, TESTS, Rig, finish, generate, opening, passed, submit
from tests.unit.test_selection_feedback import outcomes

LENIENT_BOTH = ScriptedAttempt({"calculator.py": LENIENT_CALCULATOR, "test_calculator.py": LENIENT_TESTS})
GOOD_BOTH = competition.GOOD_BOTH
SECRET_MARKS = ("test_at_", "CalculatorAcceptance", "assertRaises(ZeroDivisionError)", "not raised")


class Seeing(SimulatedLauncher):
    """Records each brief, and the files the provider could see when it started."""

    def __init__(self, script: list[ScriptedAttempt]) -> None:
        super().__init__(script)
        self.visible: list[list[str]] = []

    def start(self, request: Any) -> Any:
        session = super().start(request)
        real = session.provider.run_task

        def watch(task: Any, **kwargs: Any) -> Any:
            self.briefs.append(task.brief)
            self.visible.append(sorted(p.relative_to(task.workspace).as_posix() for p in task.workspace.rglob("*") if p.is_file()))
            return real(task, **kwargs)

        session.provider.run_task = watch  # type: ignore[method-assign]
        return session


def rig(tmp_path: Path, *steps: Any, coding: list[ScriptedAttempt], sandbox: Any = None, suite: acceptance.AcceptanceSuite | None = ACCEPTANCE_SUITE, **overrides: Any) -> Rig:
    r = Rig(tmp_path, *steps, launcher=Seeing(coding), sandbox=sandbox or ReplaySandbox.load(), **overrides)
    r.h.workspace.write_text(r.project_id, "README.md", SEED["README.md"])
    r.before = take_snapshot(r.original)
    if suite is not None:  # what repositories.create_run does for a project that has a suite
        text, sha = acceptance.freeze(suite)
        with r.h.sessions.begin() as s:
            run = s.get(Run, r.run_id)
            run.acceptance_suite_json, run.acceptance_sha256 = text, sha
    return r


def gpt_saw(r: Rig) -> str:
    return " ".join(i.output_json or "" for i in r.h.invocations(r.run_id))


def one_task() -> list[Any]:
    return competition._opening(competition._ONE_TASK_PLAN)


def test_the_evaluators_suite_runs_after_the_providers_tests_and_the_verified_code_is_delivered(tmp_path: Path) -> None:
    r = rig(tmp_path, *opening(), generate(1), generate(2), submit(), finish(), coding=[GOOD, TESTS])
    assert r.advance().state is RunState.COMPLETED
    independent = r.report()["acceptance"]
    assert independent["verdict"] == "PASSED" and independent["passed"] == 5 and independent["suite_sha256"] == acceptance.freeze(ACCEPTANCE_SUITE)[1]
    first, second = r.sandbox.specs
    assert first.workspace_dir == r.h.settings.coding_work_dir / r.run_id and second.workspace_dir == r.h.settings.coding_work_dir / "acceptance" / r.run_id
    assert not second.workspace_dir.exists() and sorted(r.h.workspace.list_files(r.project_id)) == ["README.md", "calculator.py", "test_calculator.py"]


def test_the_coding_provider_and_gpt_never_see_the_suite(tmp_path: Path) -> None:
    r = rig(tmp_path, *opening(), generate(1), generate(2), submit(), finish(), coding=[GOOD, TESTS])
    r.advance()
    seen = " ".join(r.launcher.briefs) + gpt_saw(r)
    assert not any(mark in seen for mark in SECRET_MARKS) and all("test_acceptance.py" not in files for files in r.launcher.visible)


def test_a_self_graded_pass_is_caught_and_the_repair_hears_only_the_criterion(tmp_path: Path) -> None:
    r = rig(tmp_path, *one_task(), generate(1), submit(), generate(1, "Fix divide."), submit(), finish(), coding=[LENIENT_BOTH, GOOD_BOTH])
    assert r.advance().state is RunState.COMPLETED
    report = r.report()
    assert [a["verdict"] for a in report["acceptance_runs"]] == ["FAILED", "PASSED"] and report["acceptance_runs"][0]["passed"] == 4
    assert [(d["category"], d["action"]) for d in report["recovery"]] == [("TEST_FAILURE", "REPAIR")]
    told = r.output("submit_implementation", 0)["data"]
    assert told["verdict"] == "FAILED" and told["acceptance"]["unmet"] == [{"code": "AT-005", "description": "divide(a, 0) raises ZeroDivisionError"}] and told["output_tail"] == ""
    repair_brief = r.launcher.briefs[1]
    assert "AT-005" in repair_brief and "divide(a, 0) raises ZeroDivisionError" in repair_brief
    assert not any(mark in repair_brief + gpt_saw(r) for mark in SECRET_MARKS)
    assert [o.status for o in outcomes(r)] == ["FAILED", "VERIFIED"]  # the selector learns from the independent verdict


def test_code_that_fails_acceptance_is_never_delivered(tmp_path: Path) -> None:
    r = rig(tmp_path, *one_task(), generate(1), submit(), finish(), coding=[LENIENT_BOTH])
    run = r.advance()
    assert run.state is not RunState.COMPLETED and r.untouched()  # nothing was delivered: the state gate refuses finish before the verdict check is reached
    assert r.output("finish")["error"]["summary"] == "finish is not available in state REPAIRING (allowed: DOCUMENTING)"
    assert json.loads(r.h.run(r.run_id).verification_json)["verdict"] == "FAILED"


def test_a_changed_copy_of_the_suite_stops_the_run_and_keeps_the_evidence(tmp_path: Path) -> None:
    r = rig(tmp_path, *opening(), generate(1), generate(2), submit(), coding=[GOOD, TESTS])
    with r.h.sessions.begin() as s:
        run = s.get(Run, r.run_id)
        run.acceptance_suite_json = run.acceptance_suite_json.replace("assertRaises", "assertTrue")
    run = r.advance()
    assert run.state is RunState.FAILED and run.outcome == "acceptance_suite_changed" and r.untouched() and r.candidates.exists(r.run_id)
    assert r.sandbox.specs == []


def test_an_acceptance_run_with_no_answer_delivers_nothing(tmp_path: Path) -> None:
    def respond(spec: ExecSpec) -> ExecResult:
        return ExecResult.unavailable(spec, "docker is down") if "acceptance" in spec.workspace_dir.parts else passed(spec)

    r = rig(tmp_path, *opening(), generate(1), generate(2), submit(), submit("Again."), submit("Never runs."), coding=[GOOD, TESTS], sandbox=FakeSandbox(respond))
    run = r.advance()
    assert run.state is RunState.FAILED and run.outcome == "verification_failed" and r.untouched()
    assert [d["action"] for d in r.report()["recovery"]] == ["RETRY", "STOP"] and r.report()["acceptance"]["verdict"] == "NOT_RUN"


def test_without_a_suite_nothing_changes_and_the_report_says_so(tmp_path: Path) -> None:
    r = rig(tmp_path, *opening(), generate(1), generate(2), submit(), finish(), coding=[GOOD, TESTS], suite=None)
    assert r.advance().state is RunState.COMPLETED and len(r.sandbox.specs) == 1
    assert r.report()["acceptance"] == {"configured": False, "note": "No independent acceptance suite: the verdict rests on tests written by the coding provider."}


def test_the_evaluators_tests_are_never_settled_as_the_providers_work(tmp_path: Path) -> None:
    r = rig(tmp_path, *opening(), generate(1), generate(2), submit(), finish(), coding=[GOOD, TESTS])
    r.advance()
    with r.h.sessions() as s:
        rows = s.query(ToolOutcome).filter(ToolOutcome.run_id == r.run_id).all()
    assert len(rows) == 2 and all(row.status == "VERIFIED" for row in rows)
    assert json.loads(r.h.run(r.run_id).acceptance_json)["criteria"][4]["code"] == "AT-005"


def test_a_one_stack_plan_with_a_front_end_criterion_is_blocked_before_anything_runs(tmp_path: Path) -> None:
    """The criterion's run could never happen; it is blocked with the reason, not retried and never passed."""
    from tests.unit.test_acceptance import FRONT

    r = rig(tmp_path, *opening(), generate(1), generate(2), submit(), coding=[GOOD, TESTS], suite=FRONT)
    run = r.advance()
    assert run.state is RunState.STOPPED_BY_POLICY and run.outcome == "verification_unavailable"
    assert "frontend (acceptance suite): the acceptance suite binds AT-006, AT-007 to frontend, which a one-stack plan does not have" in run.status_detail
    assert r.sandbox.specs == [] and r.untouched() and run.acceptance_json is None
