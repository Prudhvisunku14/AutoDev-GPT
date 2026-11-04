"""The final report over the API, and a run driven to the end with no Docker."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.coding.simulated import PROVIDER_NAME, ScriptedAttempt, SimulatedLauncher
from app.demo import mvp
from app.main import API_PREFIX, create_app
from app.orchestrator.fake_llm import FakeLLM, call
from app.orchestrator.llm_client import LLMErrorKind
from app.orchestrator.fake_llm import fail
from app.sandbox.fake import FakeSandbox
from app.sandbox.spec import ExecResult, ExecSpec, ExecStatus
from app.selection.selector import ProviderOption, ProviderSelector
from tests.helpers import API_TOKEN, make_settings

HEADERS = {"Authorization": f"Bearer {API_TOKEN}"}
PASSING = "".join(f"test_ac_00{n}_x (t.T.test_ac_00{n}_x) ... ok\n" for n in range(1, 6)) + "\nRan 5 tests in 0.0s\n\nOK\n"


def _passed(spec: ExecSpec) -> ExecResult:
    return ExecResult(ExecStatus.SUCCEEDED, spec.command, spec.container_name, exit_code=0, stderr=PASSING, cleanup="removed")


def _client(tmp_path: Path, llm: FakeLLM, coding: list[ScriptedAttempt] | None = None) -> TestClient:
    selector = ProviderSelector([ProviderOption(PROVIDER_NAME, SimulatedLauncher(coding or []), frozenset({"code_generation", "test_generation"}), simulated=True)])
    settings = make_settings(tmp_path, openai_price_input_per_mtok=1.0, openai_price_output_per_mtok=1.0)
    return TestClient(create_app(settings, llm=llm, sandbox=FakeSandbox(_passed), coding=selector))


def _run(client: TestClient) -> dict:
    project = client.post(f"{API_PREFIX}/projects", json={"name": "P", "requirement_text": "R"}, headers=HEADERS).json()
    run = client.post(f"{API_PREFIX}/projects/{project['id']}/runs", headers=HEADERS).json()
    client.post(f"{API_PREFIX}/runs/{run['id']}/start", headers=HEADERS).raise_for_status()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        run = client.get(f"{API_PREFIX}/runs/{run['id']}", headers=HEADERS).json()
        if run["ended_at"] or run["pause_reason"]:
            break
        time.sleep(0.02)
    return run


@pytest.fixture
def steps() -> Iterator[list]:
    yield [*mvp._calculator_opening()]


def test_a_finished_run_serves_its_report(tmp_path: Path, steps: list) -> None:
    llm = FakeLLM(*steps, call("generate_code", {"task_order": 1, "instructions": "a"}), call("generate_code", {"task_order": 2, "instructions": "b"}),
                  call("submit_implementation", {"summary": "done"}), call("finish", {"summary": "delivered"}))  # fmt: skip
    with _client(tmp_path, llm, [ScriptedAttempt({"calculator.py": mvp.CALCULATOR}), ScriptedAttempt({"test_calculator.py": mvp.CALCULATOR_TESTS})]) as client:
        run = _run(client)
        report = client.get(f"{API_PREFIX}/runs/{run['id']}/report", headers=HEADERS).json()
    assert run["state"] == "COMPLETED" and report["run"]["state"] == "COMPLETED" and report["provenance"]["fully_real"] is False
    assert report["provenance"]["gpt"] == "FakeLLM (scripted)" and report["provenance"]["sandbox"] == "FakeSandbox"


def test_a_run_still_going_has_no_report(tmp_path: Path, steps: list) -> None:
    with _client(tmp_path, FakeLLM(*steps, fail(LLMErrorKind.TRANSIENT))) as client:
        run = _run(client)
        assert run["ended_at"] is None and client.get(f"{API_PREFIX}/runs/{run['id']}/report", headers=HEADERS).status_code == 404


def test_a_run_that_ended_on_a_gpt_error_still_has_a_report(tmp_path: Path, steps: list) -> None:
    with _client(tmp_path, FakeLLM(*steps, fail(LLMErrorKind.UNEXPECTED, "boom"))) as client:
        run = _run(client)
        report = client.get(f"{API_PREFIX}/runs/{run['id']}/report", headers=HEADERS).json()
    assert run["state"] == "FAILED" and report["run"]["outcome"] == "llm_error" and report["delivered_files"] == []


def test_the_report_needs_the_token(tmp_path: Path, steps: list) -> None:
    with _client(tmp_path, FakeLLM(*steps)) as client:
        assert client.get(f"{API_PREFIX}/runs/{'a' * 32}/report").status_code == 401


def test_a_coding_provider_needs_a_sandbox(tmp_path: Path) -> None:
    selector = ProviderSelector([])
    with pytest.raises(ValueError, match="sandbox"):
        create_app(make_settings(tmp_path), llm=FakeLLM(), coding=selector)
