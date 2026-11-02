"""Failure classification. Where a failure can be produced for real, this is real unittest output on files the test wrote."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.coding.audit import audit, take_snapshot
from app.coding.types import AttemptPhase, CodingStatus
from app.recovery.diagnosis import CODING, VERIFICATION, AttemptSignal, Confidence, Diagnosis, classify_attempt, classify_refusal, classify_verification, fingerprint
from app.recovery.diagnosis import FailureCategory as Category
from app.recovery.engine import Action
from app.sandbox.spec import ExecResult, ExecStatus
from app.verification.results import Criterion, VerificationResult, judge
from tests.unit.test_coding_tools import FAILING
from tests.unit.test_verification import GOOD, TWO_MUST, _exec, _unittest

UNDONE = ("ABANDONED", "REJECTED", "RESTORED")


def run_of(folder: Path, files: dict[str, str], criteria: tuple[Criterion, ...] = ()) -> Diagnosis:
    return classify_verification(judge(_exec(_unittest(folder, files)), criteria))


def box(status: ExecStatus, output: str = "", **changes: object) -> VerificationResult:
    values: dict[str, object] = {"status": status, "command": "python-unittest", "container": "c", "exit_code": 1, "stderr": output, "cleanup": "removed", **changes}
    return judge(ExecResult(**values), ())  # type: ignore[arg-type]


def attempt(status: CodingStatus = CodingStatus.FAILED, phase: str = "RESTORED", detail: str = "it failed", violations: tuple[str, ...] = (), task: int = 1) -> Diagnosis:
    return classify_attempt(AttemptSignal(task, status, phase, detail, violations))


# --- The test run --------------------------------------------------------------------------------------


def test_a_real_syntax_error_is_named_with_its_place(tmp_path: Path) -> None:
    found = run_of(tmp_path, {"test_a.py": "def broken(:\n"})
    assert found.category is Category.SYNTAX_ERROR and found.confidence is Confidence.HIGH and found.source == VERIFICATION
    assert found.location == "test_a.py:1" and any("SyntaxError" in line for line in found.evidence) and found.actions[0] is Action.REPAIR


def test_a_real_assertion_failure_shows_what_failed(tmp_path: Path) -> None:
    found = run_of(tmp_path, {"test_a.py": GOOD + "    def test_ac_002_sub(self):\n        self.assertEqual(1, 2)\n"}, TWO_MUST)
    assert found.category is Category.TEST_FAILURE and found.failure_class == "TEST_FAILURE" and "AssertionError: 1 != 2" in found.evidence


def test_a_real_missing_module_says_nothing_can_be_installed(tmp_path: Path) -> None:
    found = run_of(tmp_path, {"test_a.py": "import nothing_called_this\n"})
    assert found.category is Category.MISSING_DEPENDENCY and found.confidence is Confidence.HIGH and found.failure_class == "IMPORT_ERROR"
    assert any("nothing_called_this" in line for line in found.evidence) and any("nothing can be installed" in line for line in found.evidence)


def test_a_real_exception_in_the_code_is_not_called_an_assertion_failure(tmp_path: Path) -> None:
    found = run_of(tmp_path, {"test_a.py": GOOD + "    def test_ac_002_boom(self):\n        raise RuntimeError('bang')\n"}, TWO_MUST)
    assert found.category is Category.RUNTIME_EXCEPTION and "RuntimeError: bang" in found.evidence and found.location == "test_a.py:6"


def test_failures_and_errors_together_count_as_a_test_failure(tmp_path: Path) -> None:
    both = GOOD + "    def test_ac_002_sub(self):\n        self.assertEqual(1, 2)\n    def test_boom(self):\n        raise RuntimeError('bang')\n"
    assert run_of(tmp_path, {"test_a.py": both}, TWO_MUST).category is Category.TEST_FAILURE


def test_a_real_empty_suite_is_invalid_code(tmp_path: Path) -> None:
    found = run_of(tmp_path, {"readme.txt": "no tests"})
    assert found.category is Category.INVALID_CODE and found.failure_class == "NO_TESTS"


def test_a_criterion_nobody_tested_is_invalid_code(tmp_path: Path) -> None:
    found = run_of(tmp_path, {"test_a.py": GOOD}, TWO_MUST)
    assert found.category is Category.INVALID_CODE and found.failure_class == "MISSING_CRITERION_TEST" and "AC-002" in found.evidence[0]


def test_tests_that_timed_out_are_fixed_in_the_code_and_their_limit_is_never_raised() -> None:
    found = classify_verification(box(ExecStatus.TIMEOUT, detail="the command ran longer than 120 seconds and was stopped"))
    assert found.category is Category.TIMEOUT and found.failure_class == "TEST_TIMEOUT" and Action.RECONFIGURE not in found.actions and found.actions[0] is Action.REPAIR


@pytest.mark.parametrize("status", [ExecStatus.MEMORY_LIMIT, ExecStatus.OUTPUT_LIMIT])
def test_the_sandbox_limits_are_resource_exhaustion(status: ExecStatus) -> None:
    found = classify_verification(box(status, detail="stopped"))
    assert found.category is Category.RESOURCE_EXHAUSTION and found.confidence is Confidence.HIGH and Action.RECONFIGURE not in found.actions


@pytest.mark.parametrize("status", [ExecStatus.UNAVAILABLE, ExecStatus.ERROR])
def test_a_sandbox_with_no_answer_is_not_blamed_on_the_code(status: ExecStatus) -> None:
    found = classify_verification(box(status, detail="docker is down"))
    assert found.category is Category.TOOL_UNAVAILABLE and found.failure_class == "ENVIRONMENT" and found.actions == (Action.RETRY, Action.STOP)


def test_a_sandbox_with_no_answer_is_the_same_failure_whatever_docker_says() -> None:
    one = classify_verification(box(ExecStatus.UNAVAILABLE, detail="No such container: abfe12"))
    two = classify_verification(box(ExecStatus.UNAVAILABLE, detail="No such container: cdea34"))
    assert one.fingerprint == two.fingerprint != classify_verification(box(ExecStatus.ERROR, detail="No such container: abfe12")).fingerprint


def test_a_coding_failure_is_the_same_failure_whatever_the_provider_says() -> None:
    one = attempt(CodingStatus.UNAVAILABLE, detail="the coding containers did not start: No such container: devpilot-opencode-abfe12")
    two = attempt(CodingStatus.UNAVAILABLE, detail="the coding containers did not start: No such container: devpilot-opencode-cdea34")
    assert one.fingerprint == two.fingerprint != attempt(CodingStatus.FAILED, detail="the coding containers did not start: No such container: devpilot-opencode-abfe12").fingerprint


def test_output_that_cannot_be_read_is_not_guessed_at(tmp_path: Path) -> None:
    done = _unittest(tmp_path, {"test_a.py": GOOD})
    found = classify_verification(judge(_exec(done, stderr_truncated=True), (Criterion("AC-001", "MUST"),)))
    assert found.category is Category.UNKNOWN and found.confidence is Confidence.LOW and Action.SWITCH_TOOL not in found.actions


def test_a_test_container_that_could_not_be_removed_fails_closed() -> None:
    found = classify_verification(box(ExecStatus.TIMEOUT, cleanup="failed", detail="stopped"))
    assert found.category is Category.AMBIGUOUS_STATE and not found.recovery_safe and found.actions == (Action.STOP,)


def test_what_a_program_prints_cannot_override_the_sandbox() -> None:
    found = classify_verification(box(ExecStatus.TIMEOUT, "SyntaxError: nothing wrong here\nModuleNotFoundError: No module named 'x'\n", detail="stopped"))
    assert found.category is Category.TIMEOUT


# --- The coding attempt --------------------------------------------------------------------------------


def test_a_timed_out_attempt_asks_for_time_first() -> None:
    found = attempt(CodingStatus.TIMEOUT, detail="no result within 600 seconds")
    assert found.category is Category.TIMEOUT and found.source == CODING and found.actions[0] is Action.RECONFIGURE and found.failure_class == "CODING_TIMEOUT"


def test_a_used_up_token_budget_cannot_be_retried_on_the_same_tool() -> None:
    found = attempt(CodingStatus.LIMIT, detail="the task used more than its budget of 50000 tokens")
    assert found.category is Category.RESOURCE_EXHAUSTION and found.actions == (Action.SWITCH_TOOL, Action.STOP)


@pytest.mark.parametrize(("status", "confidence"), [(CodingStatus.UNAVAILABLE, Confidence.HIGH), (CodingStatus.FAILED, Confidence.MEDIUM), (CodingStatus.CANCELLED, Confidence.MEDIUM)])
def test_a_provider_that_could_not_do_the_work_looks_elsewhere_first(status: CodingStatus, confidence: Confidence) -> None:
    found = attempt(status)
    assert found.category is Category.TOOL_UNAVAILABLE and found.confidence is confidence and found.actions == (Action.SWITCH_TOOL, Action.RETRY, Action.STOP)


@pytest.mark.parametrize("status", [CodingStatus.POLICY_VIOLATION, CodingStatus.AUTH, CodingStatus.INCOMPATIBLE])
def test_a_security_or_permission_failure_fails_closed(status: CodingStatus) -> None:
    found = attempt(status)
    assert found.category is Category.SECURITY_VIOLATION and not found.recovery_safe and found.actions == (Action.STOP,)


def test_a_security_failure_stays_one_whatever_its_detail_says() -> None:
    assert attempt(CodingStatus.POLICY_VIOLATION, detail="timed out, please retry").category is Category.SECURITY_VIOLATION


@pytest.mark.parametrize("status", [CodingStatus.PROTOCOL, CodingStatus.BUSY])
def test_an_answer_that_cannot_be_read_or_a_held_project_fails_closed(status: CodingStatus) -> None:
    found = attempt(status, phase="REFUSED" if status is CodingStatus.BUSY else "RESTORED")
    assert found.category is Category.AMBIGUOUS_STATE and found.actions == (Action.STOP,)


def test_a_quarantined_attempt_fails_closed_whatever_its_detail_says() -> None:
    found = attempt(CodingStatus.FAILED, phase="QUARANTINED", detail="the workspace was restored and verified")
    assert found.category is Category.AMBIGUOUS_STATE and not found.recovery_safe and found.actions == (Action.STOP,)


@pytest.mark.parametrize("status", list(CodingStatus))
@pytest.mark.parametrize("phase", [str(p) for p in AttemptPhase] + ["REFUSED", "A_PHASE_ADDED_LATER"])
def test_nothing_but_proof_that_an_attempt_was_undone_allows_recovery(status: CodingStatus, phase: str) -> None:
    found = attempt(status, phase=phase)
    if phase not in UNDONE:
        assert not found.recovery_safe and found.actions == (Action.STOP,)


def test_delivery_that_was_ambiguous_but_is_proved_undone_is_no_longer_ambiguous() -> None:
    detail = "the prompt's delivery is ambiguous (no answer; the prompt was not seen, and may still start)"
    assert attempt(CodingStatus.UNAVAILABLE, phase="RESTORED", detail=detail).recovery_safe
    assert not attempt(CodingStatus.UNAVAILABLE, phase="QUARANTINED", detail=detail).recovery_safe


def test_an_attempt_that_ended_in_no_known_way_is_treated_cautiously() -> None:
    found = attempt(CodingStatus.COMPLETED)
    assert found.category is Category.UNKNOWN and found.confidence is Confidence.LOW and Action.SWITCH_TOOL not in found.actions


# --- What the audit said: its own words, from the real audit ------------------------------------------


def audit_of(tmp_path: Path, files: dict[str, str], *, max_file_bytes: int = 100_000, max_files: int = 100, secrets: tuple[str, ...] = ()) -> tuple[str, ...]:
    root = tmp_path / "copy"
    root.mkdir()
    before = take_snapshot(root)
    for name, content in files.items():
        (root / name).write_text(content, encoding="utf-8")
    return tuple(audit(root, before, take_snapshot(root), max_file_bytes=max_file_bytes, max_files=max_files, secrets=secrets).violations)


def test_an_oversized_file_is_invalid_code_not_a_breach(tmp_path: Path) -> None:
    found = attempt(CodingStatus.POLICY_VIOLATION, violations=audit_of(tmp_path, {"big.py": "x" * 200}, max_file_bytes=100))
    assert found.category is Category.INVALID_CODE and found.recovery_safe and found.actions[0] is Action.RETRY


def test_too_many_files_is_invalid_code_not_a_breach(tmp_path: Path) -> None:
    found = attempt(CodingStatus.POLICY_VIOLATION, violations=audit_of(tmp_path, {"a.py": "1", "b.py": "2", "c.py": "3"}, max_files=2))
    assert found.category is Category.INVALID_CODE


def test_a_protected_path_is_a_breach(tmp_path: Path) -> None:
    assert attempt(CodingStatus.POLICY_VIOLATION, violations=audit_of(tmp_path, {"opencode.json": "{}"})).category is Category.SECURITY_VIOLATION


def test_a_secret_in_a_file_is_a_breach(tmp_path: Path) -> None:
    assert attempt(CodingStatus.POLICY_VIOLATION, violations=audit_of(tmp_path, {"leak.py": "KEY = 'hunter2-hunter2'"}, secrets=("hunter2-hunter2",))).category is Category.SECURITY_VIOLATION


def test_one_breach_among_oversized_files_is_still_a_breach(tmp_path: Path) -> None:
    both = audit_of(tmp_path, {"big.py": "x" * 200, "opencode.json": "{}"}, max_file_bytes=100)
    assert len(both) == 2 and attempt(CodingStatus.POLICY_VIOLATION, violations=both).category is Category.SECURITY_VIOLATION


def test_a_finding_this_module_does_not_know_stays_a_breach() -> None:
    assert attempt(CodingStatus.POLICY_VIOLATION, violations=("a finding added to the audit later",)).category is Category.SECURITY_VIOLATION


def test_an_oversized_file_in_a_quarantined_attempt_is_ambiguous(tmp_path: Path) -> None:
    found = attempt(CodingStatus.POLICY_VIOLATION, phase="QUARANTINED", violations=audit_of(tmp_path, {"big.py": "x" * 200}, max_file_bytes=100))
    assert found.category is Category.AMBIGUOUS_STATE


# --- Fingerprints --------------------------------------------------------------------------------------


def test_the_same_failure_with_other_numbers_and_paths_has_the_same_fingerprint() -> None:
    one = classify_verification(box(ExecStatus.FAILED, FAILING))
    two = classify_verification(box(ExecStatus.FAILED, FAILING.replace("-3 != 3", "-7 != 7").replace("0.001s", "0.5s")))
    assert one.fingerprint == two.fingerprint
    assert attempt(detail="cannot reach /srv/a/b at 10.0.0.1:4096").fingerprint == attempt(detail="cannot reach /srv/c at 10.0.0.2:5000").fingerprint


def test_a_different_failing_test_is_a_different_failure() -> None:
    other = FAILING.replace("test_ac_002_x", "test_ac_003_x")
    assert classify_verification(box(ExecStatus.FAILED, FAILING)).fingerprint != classify_verification(box(ExecStatus.FAILED, other)).fingerprint


def test_the_same_failure_on_another_task_is_a_different_failure() -> None:
    assert attempt(task=1).fingerprint != attempt(task=2).fingerprint


def test_a_fingerprint_depends_on_the_category() -> None:
    assert fingerprint(Category.TIMEOUT, "a", varying="b") != fingerprint(Category.TEST_FAILURE, "a", varying="b")


# --- Every diagnosis -----------------------------------------------------------------------------------


@pytest.mark.parametrize("status", list(CodingStatus))
def test_every_diagnosis_can_end_in_a_stop(status: CodingStatus) -> None:
    assert attempt(status).actions[-1] is Action.STOP


def test_evidence_is_short_and_a_diagnosis_can_be_recorded() -> None:
    found = attempt(CodingStatus.POLICY_VIOLATION, detail="x" * 5_000, violations=tuple(f"v{n}" for n in range(20)))
    assert len(found.evidence) == 5 and all(len(line) <= 200 for line in found.evidence)
    assert json.loads(json.dumps(found.to_dict()))["recovery_safe"] is False


def test_no_tool_at_all_ends_at_once_and_says_why() -> None:
    found = classify_refusal({"opencode_server": "docker is down", "gpt_writer": "it is planned and has no implementation"})
    assert found.actions == (Action.STOP,) and found.category is Category.TOOL_UNAVAILABLE and "opencode_server: docker is down" in found.evidence
    assert classify_refusal({}).evidence == ("no tool is configured",)
