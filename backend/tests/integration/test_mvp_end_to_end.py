"""The offline end-to-end demonstration, with real Docker. Opt-in: RUN_DOCKER_TESTS=true.

Real here: the API, the orchestrator, the audit, the disposable copy, the Docker containers and the
tests they run. Simulated: GPT's replies and the coding provider, which writes fixed files. So these
tests show that the workflow works. They do not show that a model or OpenCode can write the code.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.demo.mvp import ScenarioResult, run_scenario, scenarios
from app.sandbox.docker_cli import DockerCli
from app.sandbox.process import SubprocessRunner

pytestmark = pytest.mark.docker

SCENARIOS = {s.key: s for s in scenarios()}


def _run(key: str, tmp_path: Path) -> ScenarioResult:
    return run_scenario(SCENARIOS[key], tmp_path / key)


def _no_leftovers() -> bool:
    return DockerCli("docker", SubprocessRunner()).labelled_containers() == []


def test_a_requirement_becomes_tested_delivered_files(tmp_path: Path) -> None:
    result = _run("success", tmp_path)
    report = result.report
    assert result.state == "COMPLETED"
    check = report["verifications"][0]
    assert check["verdict"] == "PASSED" and check["ran"] == 5 and check["passed"] == 5 and check["exit_code"] == 0
    assert check["container"].startswith("devpilot-sbx-") and check["cleanup"] == "removed"
    assert {f["path"] for f in report["delivered_files"]} == {"calculator.py", "test_calculator.py"}
    assert all(c["status"] == "PASSED" for c in report["criteria"])
    assert result.seeded_file_intact and result.left_in_work_dir == [] and result.gpt_steps_left == 0 and _no_leftovers()


def test_every_report_says_what_was_simulated(tmp_path: Path) -> None:
    provenance = _run("success", tmp_path).report["provenance"]
    assert provenance["sandbox"] == "DockerSandbox" and provenance["fully_real"] is False
    assert provenance["simulated"] == ["GPT orchestration", "coding provider"]


def test_a_real_bug_fails_in_docker_and_the_original_workspace_is_unchanged(tmp_path: Path) -> None:
    result = _run("failed_tests", tmp_path)
    checks = result.report["verifications"]
    assert result.state == "FAILED" and result.outcome == "verification_failed"
    assert [c["verdict"] for c in checks] == ["FAILED", "FAILED"] and all(c["exit_code"] == 1 and c["failed_names"] == ["test_ac_002_subtraction"] for c in checks)
    assert result.original_unchanged and result.seeded_file_intact and result.report["delivered_files"] == []
    assert [d["action"] for d in result.report["recovery"]] == ["REPAIR", "STOP"] and result.left_in_work_dir == []


def test_a_simulated_coding_failure_is_undone_and_recovered(tmp_path: Path) -> None:
    result = _run("coding_failure", tmp_path)
    first = result.report["coding_attempts"][0]
    assert result.state == "COMPLETED" and first["status"] == "FAILED" and first["restored"] is True
    assert result.report["recovery"][0]["action"] == "RETRY" and "half_written.py" not in result.original_after


def test_a_real_bug_is_repaired_and_verified_again(tmp_path: Path) -> None:
    result = _run("repair", tmp_path)
    assert result.state == "COMPLETED" and [c["verdict"] for c in result.report["verifications"]] == ["FAILED", "PASSED"]


def test_a_policy_violation_stops_the_run_with_the_original_unchanged(tmp_path: Path) -> None:
    result = _run("policy_violation", tmp_path)
    assert result.state == "FAILED" and result.original_unchanged and result.report["verifications"] == []
    assert "opencode.json" in result.report["coding_attempts"][0]["violations"][0]


def test_a_different_requirement_takes_the_same_path(tmp_path: Path) -> None:
    result = _run("other_software", tmp_path)
    assert result.state == "COMPLETED" and {f["path"] for f in result.report["delivered_files"]} == {"wordcount.py", "test_wordcount.py"}
