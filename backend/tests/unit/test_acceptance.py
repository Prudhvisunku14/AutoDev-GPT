"""Independent acceptance on its own: the suite's checks, its freezing, its folder, and what each reader may see."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.demo.fixtures import ACCEPTANCE_SUITE, GOOD, LENIENT
from app.recovery.diagnosis import FailureCategory, classify_verification
from app.sandbox.replay import ReplaySandbox, recording_key
from app.sandbox.spec import ExecResult, ExecSpec, ExecStatus
from app.verification import acceptance
from app.verification.acceptance import AcceptanceSuite, SuiteCriterion, SuiteError
from app.verification.results import Verdict, criterion_pattern

RECORDINGS = json.loads((Path(acceptance.__file__).resolve().parents[1] / "demo" / "recordings.json").read_text(encoding="utf-8"))["recordings"]


def recorded(name: str) -> ExecResult:
    r = next(r for r in RECORDINGS if r["name"] == name)
    return ExecResult(ExecStatus(r["status"]), "python-unittest", "c", r["exit_code"], stdout=r["stdout"], stderr=r["stderr"], cleanup="removed")


# --- The suite's own checks ----------------------------------------------------------------------------------------


def test_the_demonstration_suite_is_valid() -> None:
    acceptance.validate(ACCEPTANCE_SUITE)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda s: replace(s, criteria=(*s.criteria, SuiteCriterion("AT-006", "not tested"))), "no test names AT-006"),
        (lambda s: replace(s, criteria=(SuiteCriterion("AC-001", "a developer code"),)), "look like AT-001"),
        (lambda s: replace(s, criteria=(s.criteria[0], s.criteria[0])), "unique"),
        (lambda s: replace(s, files={"tests/test_x.py": "def test_at_001(): pass"}), "no folder"),
        (lambda s: replace(s, files={"helper.py": "def test_at_001(): pass"}), "test_<name>.py"),
        (lambda s: replace(s, files={"test_x.py": "def test_at_001(:"}), "not valid Python"),
        (lambda s: replace(s, criteria=(replace(s.criteria[0], priority="COULD"),)), "MUST or SHOULD"),
    ],
)
def test_a_suite_that_cannot_prove_its_criteria_is_refused(change: object, message: str) -> None:
    with pytest.raises(SuiteError, match=message):
        acceptance.validate(change(ACCEPTANCE_SUITE))  # type: ignore[operator]


def test_checking_a_suite_never_runs_it(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    source = f"open({str(marker)!r}, 'w').write('x')\n" + "".join(f"def test_at_00{n}(): pass\n" for n in range(1, 6))
    acceptance.validate(replace(ACCEPTANCE_SUITE, files={"test_x.py": source}))
    assert not marker.exists()


def test_a_frozen_suite_comes_back_unchanged_and_a_changed_copy_is_refused() -> None:
    text, sha = acceptance.freeze(ACCEPTANCE_SUITE)
    assert acceptance.thaw(text, sha) == AcceptanceSuite.from_dict(ACCEPTANCE_SUITE.to_dict())
    with pytest.raises(SuiteError, match="no longer matches"):
        acceptance.thaw(text.replace("assertRaises", "assertTrue"), sha)


def test_freezing_is_deterministic() -> None:
    assert acceptance.freeze(ACCEPTANCE_SUITE) == acceptance.freeze(AcceptanceSuite.from_dict(json.loads(json.dumps(ACCEPTANCE_SUITE.to_dict()))))


# --- The folder the evaluator's tests run in ------------------------------------------------------------------------


def test_the_providers_tests_are_left_out_and_the_evaluators_go_in_last() -> None:
    candidate = {"calculator.py": b"x", "test_calculator.py": b"t", "tests.py": b"t", "pkg/test_inner.py": b"t", "test_acceptance.py": b"forged", "README.md": b"r"}
    files = acceptance.evaluation_files(candidate, ACCEPTANCE_SUITE)
    assert set(files) == {"calculator.py", "README.md", "test_acceptance.py"}
    assert files["test_acceptance.py"] == ACCEPTANCE_SUITE.files["test_acceptance.py"].encode()


def test_the_folder_is_made_fresh_and_removed(tmp_path: Path) -> None:
    from app.tools.adapters.workspace_fs import WorkspaceFS

    fs = WorkspaceFS(tmp_path / "acceptance", max_file_bytes=100_000, max_files=50)
    root = acceptance.stage(fs, "a" * 32, {"stale.py": b"x"})
    root = acceptance.stage(fs, "a" * 32, {"calculator.py": b"y"})
    assert sorted(p.name for p in root.iterdir()) == ["calculator.py"]
    acceptance.discard(fs, "a" * 32)
    assert not root.exists()


# --- Judging -------------------------------------------------------------------------------------------------------


def test_the_real_calculator_passes_every_criterion() -> None:
    result = acceptance.judge_acceptance(recorded("calculator: the evaluator's acceptance suite"), ACCEPTANCE_SUITE, "sha")
    assert result.verdict == acceptance.PASSED and result.passed == 5 and all(c.status == "PASSED" for c in result.criteria)


def test_a_self_graded_pass_fails_the_criterion_it_dodged_and_partial_completion_is_reported() -> None:
    result = acceptance.judge_acceptance(recorded("lenient division: the evaluator's acceptance suite"), ACCEPTANCE_SUITE, "sha")
    data = result.to_dict()
    assert result.verdict == acceptance.FAILED and result.passed == 4 and data["partial"] is True and data["total"] == 5
    failed = [c for c in result.criteria if c.status != "PASSED"]
    assert [c.code for c in failed] == ["AT-005"] and "ZeroDivisionError not raised" in failed[0].evidence and "AT-005" in result.reason


def test_a_failing_should_criterion_is_reported_but_does_not_fail_acceptance() -> None:
    relaxed = replace(ACCEPTANCE_SUITE, criteria=(*ACCEPTANCE_SUITE.criteria[:4], replace(ACCEPTANCE_SUITE.criteria[4], priority="SHOULD")))
    result = acceptance.judge_acceptance(recorded("lenient division: the evaluator's acceptance suite"), relaxed, "sha")
    assert result.verdict == acceptance.PASSED and result.passed == 4 and "MUST" in result.reason


def test_a_sandbox_with_no_answer_says_nothing_about_the_code() -> None:
    spec = ExecSpec("python-unittest", "img", ("python",), Path("."), 10.0, "c")
    result = acceptance.judge_acceptance(ExecResult.unavailable(spec, "docker is down"), ACCEPTANCE_SUITE, "sha")
    assert result.verdict == acceptance.NOT_RUN and result.passed == 0


# --- What each reader may see --------------------------------------------------------------------------------------


def test_recovery_and_gpt_see_the_unmet_criteria_and_nothing_of_the_tests() -> None:
    result = acceptance.judge_acceptance(recorded("lenient division: the evaluator's acceptance suite"), ACCEPTANCE_SUITE, "sha")
    seen = acceptance.for_recovery(result, "digest")
    shown = json.dumps([seen.to_dict(), result.for_gpt()])
    assert seen.verdict is Verdict.FAILED and seen.counts.failed_names == ("AT-005",) and seen.output_tail == "" and seen.fingerprint == "digest"
    assert "test_at_" not in shown and "assertRaises" not in shown and "not raised" not in shown and "Traceback" not in shown
    assert "divide(a, 0) raises ZeroDivisionError" in seen.reason
    assert classify_verification(seen).category is FailureCategory.TEST_FAILURE


def test_an_acceptance_run_with_no_answer_is_not_a_code_failure() -> None:
    spec = ExecSpec("python-unittest", "img", ("python",), Path("."), 10.0, "c")
    seen = acceptance.for_recovery(acceptance.judge_acceptance(ExecResult.unavailable(spec, "down"), ACCEPTANCE_SUITE, "sha"), "d")
    assert seen.verdict is Verdict.NOT_RUN and classify_verification(seen).category is FailureCategory.TOOL_UNAVAILABLE


def test_an_acceptance_criterion_needs_its_own_prefix_and_developer_criteria_are_unchanged() -> None:
    assert criterion_pattern("AT-002").search("test_at_002_subtracts") and not criterion_pattern("AT-002").search("test_ac_002_subtracts")
    assert criterion_pattern("AC-002").search("test_ac_002_x") and criterion_pattern("AC-002").search("test_AC002") and not criterion_pattern("AC-002").search("test_ac_0021")


def test_the_recordings_used_here_are_for_the_fixture_trees() -> None:
    sandbox = ReplaySandbox.load()
    assert len(sandbox._recordings) == len(RECORDINGS)
    keys = {r["key"] for r in RECORDINGS}
    assert recording_key("python-unittest", ("python", "-I", "-B", "-m", "unittest", "discover", "-v", "-s", ".", "-p", "test*.py"), acceptance.evaluation_files(GOOD, ACCEPTANCE_SUITE)) in keys
    assert recording_key("python-unittest", ("python", "-I", "-B", "-m", "unittest", "discover", "-v", "-s", ".", "-p", "test*.py"), LENIENT) in keys
