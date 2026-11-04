from __future__ import annotations

import io
import zipfile

import pytest
from fastapi import HTTPException

from app.api.artifacts import describe, send
from app.core.redaction import Redactor
from app.verification import artifact
from app.verification.artifact import build_zip, select, unavailable_reason, verification_level


def report(*, simulated: list[str] | None = None) -> dict:
    return {
        "project": {"id": "p1", "name": "Attendance tracker", "requirement": "A CLI application", "stack": "python-cli"},
        "run": {"id": "12345678-run", "state": "COMPLETED", "outcome": "success", "detail": None, "turns": 2, "tokens": 10, "cost_usd": 0.01, "repairs": 0},
        "acceptance": {"configured": True, "verdict": "PASSED", "reason": "all criteria passed", "suite_sha256": "abc", "passed": 2, "total": 2, "criteria": []},
        "delivered_files": [{"path": "app.py", "change": "created", "bytes": 20}],
        "provenance": {"gpt": "real", "coding_providers": ["opencode"], "sandbox": "DockerSandbox", "simulated": simulated or [], "fully_real": not simulated},
        "tasks": [],
        "verifications": [],
        "criteria": [],
        "acceptance_runs": [],
        "recovery": [],
    }


def test_only_independently_verified_real_runs_are_downloadable() -> None:
    assert unavailable_reason(None) == "the run has no final report yet"
    assert unavailable_reason(report()) is None
    reason = unavailable_reason(report(simulated=["FakeLLM"]))
    assert reason is not None and "simulated components" in reason


def freeform(*, verdict: str = "PASSED", ran: int = 6, simulated: list[str] | None = None) -> dict:
    """A dashboard build: no evaluator's suite, only the project's own tests."""
    base = report(simulated=simulated)
    base["acceptance"] = {"configured": False, "note": "No independent acceptance suite"}
    base["verifications"] = [{"verdict": verdict, "passed": ran if verdict == "PASSED" else 0, "ran": ran}]
    return base


def test_a_freeform_build_is_downloadable_only_when_its_own_tests_passed_and_is_labelled_so() -> None:
    assert unavailable_reason(freeform()) is None
    assert verification_level(freeform()) == "generated-tests"
    assert verification_level(report()) == "independent"
    assert verification_level(freeform(verdict="FAILED")) is None
    reason = unavailable_reason(freeform(verdict="FAILED"))
    assert reason is not None and "did not pass" in reason
    assert unavailable_reason(freeform(ran=0)) is not None  # a pass with no test run proves nothing
    unrun = freeform()
    unrun["verifications"] = []
    assert unavailable_reason(unrun) is not None


def test_the_looser_tier_never_applies_to_a_failed_suite_a_simulated_run_or_an_unfinished_run() -> None:
    failed_suite = freeform()
    failed_suite["acceptance"] = {"configured": True, "verdict": "FAILED"}
    assert "did not pass" in (unavailable_reason(failed_suite) or "")  # its own passing tests do not outvote the evaluator
    assert "simulated components" in (unavailable_reason(freeform(simulated=["GPT orchestration"])) or "")
    cancelled = freeform()
    cancelled["run"]["state"] = "CANCELLED"
    assert unavailable_reason(cancelled) is not None
    empty = freeform()
    empty["delivered_files"] = []
    assert unavailable_reason(empty) == "the run delivered no files"


def test_the_manifest_says_how_a_freeform_delivery_was_verified() -> None:
    redactor = Redactor([])
    selection, data = select({"app.py": b"print(1)"}, {"app.py": b"print(1)"}.__getitem__, redactor)
    with zipfile.ZipFile(io.BytesIO(build_zip(freeform(), selection, data, redactor))) as bundle:
        manifest = bundle.read(next(n for n in bundle.namelist() if n.endswith("MANIFEST.txt"))).decode()
    assert "Verified by the generated tests only (no independent acceptance suite): 6 of 6 passed" in manifest
    assert "Independent acceptance: None" not in manifest


def test_archive_excludes_secrets_and_includes_project_and_verification_files() -> None:
    contents = {
        ".env": b"PRIVATE_VALUE=unsafe",
        ".env.example": b"PRIVATE_VALUE=placeholder",
        "README.md": b"Project documentation",
        "requirements.txt": b"dependency==1.0",
        "tests/test_app.py": b"assert True",
        "credentials.txt": b"configured-secret-value",
    }
    redactor = Redactor(["configured-secret-value"])
    selection, data = select(contents, contents.__getitem__, redactor)
    assert {entry.path for entry in selection.included} == {".env.example", "README.md", "requirements.txt", "tests/test_app.py"}
    assert {entry.path for entry in selection.excluded} == {".env", "credentials.txt"}

    archive = build_zip(report(), selection, data, redactor)
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        names = set(bundle.namelist())
        assert any(name.endswith("/README.md") for name in names)
        assert any(name.endswith("/requirements.txt") for name in names)
        assert any(name.endswith("/tests/test_app.py") for name in names)
        assert any(name.endswith("/devpilot-verification/report.json") for name in names)
        assert not any(name.endswith("/.env") for name in names)
        assert b"configured-secret-value" not in bundle.read(next(name for name in names if name.endswith("/devpilot-verification/report.json")))


@pytest.mark.parametrize("path", ["env/lib.py", "build/generated.py", "coverage/acceptance.py"])
def test_source_directories_are_not_dropped_from_verified_downloads(path: str) -> None:
    selection, data = select({path}, {path: b"print('verified')" }.__getitem__, Redactor([]))

    assert [entry.path for entry in selection.included] == [path]
    assert data[path] == b"print('verified')"


def test_secret_shaped_verified_tests_block_the_archive_instead_of_being_silently_removed() -> None:
    source = {"tests/test_auth.py": b"assert token.startswith('sk-" + b"a" * 24 + b"')"}
    selection, _ = select(source, source.__getitem__, Redactor([]))

    assert selection.excluded
    assert "verified source" in (artifact.omitted_source_reason(selection) or "")
    available = describe(report(), list(source), source.__getitem__, Redactor([]))
    assert not available.available and "verified source" in (available.reason or "")
    with pytest.raises(HTTPException) as error:
        send(report(), list(source), source.__getitem__, Redactor([]))
    assert error.value.status_code == 409
