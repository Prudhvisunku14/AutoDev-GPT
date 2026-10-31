"""Reading test runs. The output here comes from the real unittest runner, run offline in a temp folder."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from app.recovery.engine import Action, decide_coding, decide_verification
from app.coding.types import CodingStatus
from app.sandbox.spec import ExecResult, ExecStatus
from app.verification.report import render_markdown
from app.verification.results import Criterion, CriterionStatus, TestCounts, Verdict, failure_kind, judge, judge_criteria, parse_unittest

GOOD = "import unittest\nclass T(unittest.TestCase):\n    def test_ac_001_add(self):\n        self.assertEqual(1 + 1, 2)\n"
TWO_MUST = (Criterion("AC-001", "MUST"), Criterion("AC-002", "MUST"))


def _unittest(folder: Path, files: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """The sandbox's command, on the host, on files this test wrote. It runs nothing else."""
    folder.mkdir(exist_ok=True)
    for name, content in files.items():
        (folder / name).write_text(content, encoding="utf-8")
    argv = [sys.executable, "-I", "-B", "-m", "unittest", "discover", "-v", "-s", ".", "-p", "test*.py"]
    return subprocess.run(argv, cwd=folder, capture_output=True, text=True, timeout=60)


def _exec(done: subprocess.CompletedProcess[str], **changes: object) -> ExecResult:
    status = ExecStatus.SUCCEEDED if done.returncode == 0 else ExecStatus.FAILED
    values: dict[str, object] = {"status": status, "command": "python-unittest", "container": "c", "exit_code": done.returncode, "stdout": done.stdout, "stderr": done.stderr, "cleanup": "removed", **changes}
    return ExecResult(**values)  # type: ignore[arg-type]


def test_a_real_passing_run_passes(tmp_path: Path) -> None:
    result = judge(_exec(_unittest(tmp_path, {"test_a.py": GOOD})), (Criterion("AC-001", "MUST"),))
    assert result.verdict is Verdict.PASSED and result.counts.ran == 1 and result.counts.passed == 1
    assert result.criteria[0].status is CriterionStatus.PASSED and result.criteria[0].tests == ("test_ac_001_add",)


def test_a_real_failure_is_named(tmp_path: Path) -> None:
    bad = GOOD + "    def test_ac_002_sub(self):\n        self.assertEqual(1, 2)\n"
    result = judge(_exec(_unittest(tmp_path, {"test_a.py": bad})), TWO_MUST)
    assert result.verdict is Verdict.FAILED and result.counts.failures == 1 and result.counts.failed_names == ("test_ac_002_sub",)
    assert [c.status for c in result.criteria] == [CriterionStatus.PASSED, CriterionStatus.FAILED]
    assert failure_kind(result) == "TEST_FAILURE"


def test_an_error_counts_as_a_failure(tmp_path: Path) -> None:
    bad = GOOD + "    def test_ac_002_boom(self):\n        raise RuntimeError('x')\n"
    result = judge(_exec(_unittest(tmp_path, {"test_a.py": bad})), TWO_MUST)
    assert result.verdict is Verdict.FAILED and result.counts.errors == 1 and "test_ac_002_boom" in result.counts.failed_names


def test_an_empty_suite_is_never_a_pass(tmp_path: Path) -> None:
    result = judge(_exec(_unittest(tmp_path, {"readme.txt": "no tests"})), ())
    assert result.verdict is Verdict.FAILED and result.counts.ran == 0 and failure_kind(result) == "NO_TESTS"


def test_a_test_file_with_a_syntax_error_is_classified(tmp_path: Path) -> None:
    result = judge(_exec(_unittest(tmp_path, {"test_a.py": "def broken(:\n"})), ())
    assert result.verdict is Verdict.FAILED and failure_kind(result) == "SYNTAX_ERROR"


def test_a_missing_import_is_classified(tmp_path: Path) -> None:
    result = judge(_exec(_unittest(tmp_path, {"test_a.py": "import nothing_called_this\n"})), ())
    assert result.verdict is Verdict.FAILED and failure_kind(result) == "IMPORT_ERROR"


def test_a_skipped_test_proves_nothing(tmp_path: Path) -> None:
    skipped = "import unittest\nclass T(unittest.TestCase):\n    @unittest.skip('later')\n    def test_ac_001_add(self):\n        pass\n    def test_other(self):\n        pass\n"
    result = judge(_exec(_unittest(tmp_path, {"test_a.py": skipped})), (Criterion("AC-001", "MUST"),))
    assert result.verdict is Verdict.FAILED and result.counts.skipped == 1 and result.criteria[0].status is CriterionStatus.UNVERIFIED


def test_a_docstring_on_a_test_does_not_hide_it(tmp_path: Path) -> None:
    documented = 'import unittest\nclass T(unittest.TestCase):\n    def test_ac_001_add(self):\n        """Adds two numbers."""\n        self.assertEqual(1, 1)\n'
    result = judge(_exec(_unittest(tmp_path, {"test_a.py": documented})), (Criterion("AC-001", "MUST"),))
    assert result.verdict is Verdict.PASSED


def test_a_must_criterion_without_a_test_fails_a_green_run(tmp_path: Path) -> None:
    result = judge(_exec(_unittest(tmp_path, {"test_a.py": GOOD})), TWO_MUST)
    assert result.verdict is Verdict.FAILED and "AC-002" in result.reason and failure_kind(result) == "MISSING_CRITERION_TEST"


def test_a_should_criterion_without_a_test_does_not_block(tmp_path: Path) -> None:
    result = judge(_exec(_unittest(tmp_path, {"test_a.py": GOOD})), (Criterion("AC-001", "MUST"), Criterion("AC-002", "SHOULD")))
    assert result.verdict is Verdict.PASSED and result.criteria[1].status is CriterionStatus.UNVERIFIED


def test_truncated_output_is_never_read_as_a_pass(tmp_path: Path) -> None:
    result = judge(_exec(_unittest(tmp_path, {"test_a.py": GOOD}), stderr_truncated=True), (Criterion("AC-001", "MUST"),))
    assert result.verdict is Verdict.FAILED and "could not be read" in result.reason


def test_a_clean_exit_with_no_summary_is_not_a_pass() -> None:
    result = judge(ExecResult(ExecStatus.SUCCEEDED, "c", "n", exit_code=0, stderr="nothing useful"), ())
    assert result.verdict is Verdict.FAILED


def test_a_summary_saying_ok_with_a_failing_exit_is_not_a_pass() -> None:
    forged = "test_ac_001_x (a.T.test_ac_001_x) ... ok\n\nRan 1 test in 0.0s\n\nOK\n"
    result = judge(ExecResult(ExecStatus.FAILED, "c", "n", exit_code=1, stderr=forged), (Criterion("AC-001", "MUST"),))
    assert result.verdict is Verdict.FAILED


def test_a_runner_that_reports_ok_with_no_tests_is_not_a_pass() -> None:
    result = judge(ExecResult(ExecStatus.SUCCEEDED, "c", "n", exit_code=0, stderr="\nRan 0 tests in 0.000s\n\nOK\n"), ())
    assert result.verdict is Verdict.FAILED and result.reason == "no tests ran"


def test_a_failed_summary_with_a_clean_exit_is_not_a_pass() -> None:
    output = "test_ac_001_x (a.T.test_ac_001_x) ... ok\n\nRan 1 test in 0.0s\n\nFAILED (failures=1)\n"
    result = judge(ExecResult(ExecStatus.SUCCEEDED, "c", "n", exit_code=0, stderr=output), (Criterion("AC-001", "MUST"),))
    assert result.verdict is Verdict.FAILED


def test_a_nonzero_status_can_never_pass_on_exit_code_alone() -> None:
    summary = "test_ac_001_x (a.T.test_ac_001_x) ... ok\n\nRan 1 test in 0.0s\n\nOK\n"
    result = judge(ExecResult(ExecStatus.SUCCEEDED, "c", "n", exit_code=2, stderr=summary), (Criterion("AC-001", "MUST"),))
    assert result.verdict is Verdict.FAILED


@pytest.mark.parametrize("status", [ExecStatus.UNAVAILABLE, ExecStatus.ERROR, ExecStatus.CANCELLED])
def test_a_sandbox_that_gave_no_answer_says_nothing_about_the_code(status: ExecStatus) -> None:
    result = judge(ExecResult(status, "c", "n", detail="docker is down"), ())
    assert result.verdict is Verdict.NOT_RUN and failure_kind(result) == "ENVIRONMENT"


@pytest.mark.parametrize(("status", "kind"), [(ExecStatus.TIMEOUT, "TEST_TIMEOUT"), (ExecStatus.MEMORY_LIMIT, "RESOURCE_LIMIT"), (ExecStatus.OUTPUT_LIMIT, "RESOURCE_LIMIT")])
def test_limits_are_failures_of_the_code(status: ExecStatus, kind: str) -> None:
    result = judge(ExecResult(status, "c", "n", detail="stopped"), ())
    assert result.verdict is Verdict.FAILED and failure_kind(result) == kind and result.reason == "stopped"


def test_a_criterion_with_a_failing_and_a_passing_test_is_failed() -> None:
    counts = TestCounts(readable=True, ran=2, failures=1, passed_names=("test_ac_001_a",), failed_names=("test_ac_001_b",))
    assert judge_criteria((Criterion("AC-001", "MUST"),), counts)[0].status is CriterionStatus.FAILED


def test_a_test_name_may_drop_the_underscore() -> None:
    counts = TestCounts(readable=True, ran=1, passed_names=("test_AC001_x",))
    assert judge_criteria((Criterion("AC-001", "MUST"),), counts)[0].status is CriterionStatus.PASSED


def test_a_code_does_not_match_a_longer_number_by_accident() -> None:
    counts = TestCounts(readable=True, ran=1, passed_names=("test_ac_0011_x",))
    assert judge_criteria((Criterion("AC-001", "MUST"),), counts)[0].status is CriterionStatus.UNVERIFIED


def test_parse_of_nothing_is_unreadable() -> None:
    assert parse_unittest("").readable is False


# --- Recovery ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["TEST_FAILURE", "SYNTAX_ERROR", "IMPORT_ERROR", "NO_TESTS", "MISSING_CRITERION_TEST", "TEST_TIMEOUT", "RESOURCE_LIMIT"])
def test_a_code_failure_may_be_repaired_until_the_limit(kind: str) -> None:
    allowed = decide_verification(kind, repairs_used=1, max_repairs=2)
    stopped = decide_verification(kind, repairs_used=2, max_repairs=2)
    assert allowed.action is Action.REPAIR and allowed.hint
    assert stopped.action is Action.STOP and "limit" in stopped.reason


def test_no_repair_is_ever_allowed_when_the_limit_is_zero() -> None:
    assert decide_verification("TEST_FAILURE", repairs_used=0, max_repairs=0).action is Action.STOP


def test_an_environment_failure_is_not_repaired() -> None:
    assert decide_verification("ENVIRONMENT", repairs_used=0, max_repairs=5).action is Action.STOP


@pytest.mark.parametrize("status", [CodingStatus.FAILED, CodingStatus.TIMEOUT, CodingStatus.UNAVAILABLE, CodingStatus.BUSY, CodingStatus.CANCELLED])
def test_a_coding_failure_may_be_retried_until_the_limit(status: CodingStatus) -> None:
    assert decide_coding(status, quarantined=False, attempts_used=2, max_attempts=3).action is Action.RETRY
    assert decide_coding(status, quarantined=False, attempts_used=3, max_attempts=3).action is Action.STOP


@pytest.mark.parametrize("status", [CodingStatus.POLICY_VIOLATION, CodingStatus.AUTH, CodingStatus.INCOMPATIBLE, CodingStatus.PROTOCOL, CodingStatus.LIMIT])
def test_a_refusal_is_never_retried(status: CodingStatus) -> None:
    assert decide_coding(status, quarantined=False, attempts_used=0, max_attempts=3).action is Action.STOP


def test_a_quarantine_is_never_retried() -> None:
    decision = decide_coding(CodingStatus.FAILED, quarantined=True, attempts_used=0, max_attempts=3)
    assert decision.action is Action.STOP and "person" in decision.reason


# --- The report -------------------------------------------------------------------------------------


def _report(**provenance: object) -> dict[str, object]:
    return {
        "project": {"name": "P", "requirement": "R"}, "run": {"state": "FAILED", "outcome": "verification_failed", "detail": "d"},
        "tasks": [{"order": 1, "title": "T", "status": "DONE"}], "criteria": [{"code": "AC-001", "priority": "MUST", "status": "FAILED"}],
        "verifications": [{"verdict": "FAILED", "passed": 0, "ran": 1, "exit_code": 1, "container": "c", "reason": "r", "failed_names": ["test_a"]}],
        "recovery": [{"failure_class": "TEST_FAILURE", "action": "STOP", "reason": "limit"}], "delivered_files": [],
        "provenance": {"simulated": ["coding provider"], **provenance},
    }  # fmt: skip


def test_the_report_says_what_was_simulated() -> None:
    text = render_markdown(_report())
    assert "Simulated in this run:** coding provider" in text and "failed: test_a" in text and "Nothing was delivered" in text


def test_a_fully_real_run_says_so() -> None:
    assert "Nothing in this run was simulated" in render_markdown(_report(simulated=[]))
