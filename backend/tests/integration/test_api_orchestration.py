"""Phase 2 API: start/resume, approvals, run details and the SSE stream, with FakeLLM."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.demo.calculator import ANALYSIS, CRITERIA, PLAN, calculator_llm, run_demo
from app.main import create_app
from app.orchestrator.fake_llm import FakeLLM, call, fail
from app.orchestrator.llm_client import LLMErrorKind
from tests.helpers import API_TOKEN, FAKE_OPENAI_KEY, make_settings

API = "/api/v1"


@pytest.fixture
def fake_llm() -> FakeLLM:
    return calculator_llm()


@pytest.fixture
def app(tmp_path: Path, fake_llm: FakeLLM) -> FastAPI:
    return create_app(make_settings(tmp_path, sse_poll_seconds=0.02, sse_max_stream_seconds=5), llm=fake_llm)


def _new_run(client: TestClient, auth: dict[str, str], requirement: str = "Create a Python calculator application.") -> str:
    project = client.post(f"{API}/projects", json={"name": "Calc", "requirement_text": requirement}, headers=auth).json()
    return client.post(f"{API}/projects/{project['id']}/runs", headers=auth).json()["id"]


def _wait(client: TestClient, auth: dict[str, str], run_id: str, until: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        run = client.get(f"{API}/runs/{run_id}", headers=auth).json()
        if until(run):
            return run
        time.sleep(0.02)
    raise AssertionError(f"run did not reach the expected state: {run}")


def _settled(run: dict[str, Any]) -> bool:
    return bool(run["pause_reason"] or run["ended_at"] or run["state"] == "AWAITING_APPROVAL")


def test_start_without_openai_config_is_503_and_safe(tmp_path: Path, auth: dict[str, str]) -> None:
    app = create_app(make_settings(tmp_path, openai_api_key=FAKE_OPENAI_KEY, openai_model=None))
    with TestClient(app) as client:
        run_id = _new_run(client, auth)
        response = client.post(f"{API}/runs/{run_id}/start", headers=auth)
        assert response.status_code == 503
        assert "OPENAI_MODEL is not set" in response.json()["detail"]
        assert FAKE_OPENAI_KEY not in response.text
        assert client.get(f"{API}/runs/{run_id}", headers=auth).json()["state"] == "CREATED"


def test_calculator_workflow_over_the_api(client: TestClient, auth: dict[str, str], fake_llm: FakeLLM) -> None:
    run_id = _new_run(client, auth)
    started = client.post(f"{API}/runs/{run_id}/start", headers=auth)
    assert started.status_code == 202
    run = _wait(client, auth, run_id, _settled)

    assert run["state"] == "IMPLEMENTING"
    assert run["pause_reason"] == "phase_unavailable"
    assert run["turns"] == 4 and run["tool_calls"] == 4

    analysis = client.get(f"{API}/runs/{run_id}/analysis", headers=auth).json()
    assert [r["code"] for r in analysis["requirements"]] == [r["code"] for r in ANALYSIS["requirements"]]
    plan = client.get(f"{API}/runs/{run_id}/plan", headers=auth).json()
    assert plan["stack"] == "python-cli" and len(plan["tasks"]) == len(PLAN["tasks"])
    assert plan["tasks"][0]["features"]["language"] == "python"
    criteria = client.get(f"{API}/runs/{run_id}/criteria", headers=auth).json()
    assert [c["code"] for c in criteria] == [c["code"] for c in CRITERIA["criteria"]]
    invocations = client.get(f"{API}/runs/{run_id}/invocations", headers=auth).json()
    assert [(i["tool"], i["ok"]) for i in invocations] == [
        ("submit_analysis", True), ("submit_plan", False), ("submit_plan", True), ("submit_criteria", True)
    ]
    assert invocations[1]["output"]["error"]["class"] == "VALIDATION_FAILED"
    assert fake_llm.remaining == 0

    # Resuming does not call GPT again while IMPLEMENTING is unavailable.
    assert client.post(f"{API}/runs/{run_id}/start", headers=auth).status_code == 202
    time.sleep(0.2)
    assert len(fake_llm.requests) == 4


def test_details_are_404_before_they_exist(client: TestClient, auth: dict[str, str]) -> None:
    run_id = _new_run(client, auth)
    for part in ("analysis", "plan"):
        assert client.get(f"{API}/runs/{run_id}/{part}", headers=auth).status_code == 404
    assert client.get(f"{API}/runs/{run_id}/criteria", headers=auth).json() == []
    assert client.get(f"{API}/runs/{run_id}/invocations", headers=auth).json() == []


def test_start_conflicts(client: TestClient, auth: dict[str, str]) -> None:
    run_id = _new_run(client, auth)
    client.post(f"{API}/runs/{run_id}/cancel", headers=auth)
    assert client.post(f"{API}/runs/{run_id}/start", headers=auth).status_code == 409
    assert client.post(f"{API}/runs/{'f' * 32}/start", headers=auth).status_code == 404


@pytest.mark.parametrize("fake_llm", [FakeLLM(call("request_human_input", {"question": "Need a GUI?", "reason": "Unclear UI"}), call("submit_analysis", ANALYSIS), fail(LLMErrorKind.TRANSIENT))])
def test_human_input_over_the_api(client: TestClient, auth: dict[str, str], fake_llm: FakeLLM) -> None:
    run_id = _new_run(client, auth)
    client.post(f"{API}/runs/{run_id}/start", headers=auth)
    run = _wait(client, auth, run_id, _settled)
    assert run["state"] == "AWAITING_APPROVAL"
    assert client.post(f"{API}/runs/{run_id}/start", headers=auth).status_code == 409

    (pending,) = client.get(f"{API}/approvals", headers=auth).json()
    assert pending["kind"] == "HUMAN_INPUT"
    assert pending["payload"]["question"] == "Need a GUI?"
    url = f"{API}/approvals/{pending['id']}/decision"
    assert client.post(url, json={"decision": "approve"}, headers=auth).status_code == 422
    assert client.post(url, json={"decision": "maybe"}, headers=auth).status_code == 422
    decided = client.post(url, json={"decision": "approve", "response": "No GUI, terminal only."}, headers=auth)
    assert decided.status_code == 200 and decided.json()["status"] == "APPROVED"
    assert client.post(url, json={"decision": "deny"}, headers=auth).status_code == 409  # decided once

    run = _wait(client, auth, run_id, lambda r: r["state"] == "PLANNING" and _settled(r))
    assert run["pause_reason"] == "llm_error:transient"
    assert client.get(f"{API}/approvals", headers=auth).json() == []
    assert len(client.get(f"{API}/approvals?status=all&run_id={run_id}", headers=auth).json()) == 1


@pytest.mark.parametrize("fake_llm", [FakeLLM(call("request_human_input", {"question": "Q?", "reason": "R"}))])
def test_cancel_closes_pending_approvals(client: TestClient, auth: dict[str, str]) -> None:
    run_id = _new_run(client, auth)
    client.post(f"{API}/runs/{run_id}/start", headers=auth)
    _wait(client, auth, run_id, _settled)
    (pending,) = client.get(f"{API}/approvals", headers=auth).json()
    assert client.post(f"{API}/runs/{run_id}/cancel", headers=auth).json()["state"] == "CANCELLED"
    (closed,) = client.get(f"{API}/approvals?status=cancelled", headers=auth).json()
    assert closed["id"] == pending["id"]
    response = client.post(f"{API}/approvals/{pending['id']}/decision", json={"decision": "approve", "response": "x"}, headers=auth)
    assert response.status_code == 409
    assert client.get(f"{API}/runs/{run_id}/invocations", headers=auth).json()[0]["status"] == "SKIPPED"


def test_unknown_approval_is_404(client: TestClient, auth: dict[str, str]) -> None:
    for approval_id in ("f" * 32, "bad"):
        response = client.post(f"{API}/approvals/{approval_id}/decision", json={"decision": "deny"}, headers=auth)
        assert response.status_code == 404


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", f"{API}/runs/{'0' * 32}/start"),
        ("GET", f"{API}/runs/{'0' * 32}/analysis"),
        ("GET", f"{API}/runs/{'0' * 32}/plan"),
        ("GET", f"{API}/runs/{'0' * 32}/criteria"),
        ("GET", f"{API}/runs/{'0' * 32}/invocations"),
        ("GET", f"{API}/runs/{'0' * 32}/events/stream"),
        ("GET", f"{API}/approvals"),
        ("POST", f"{API}/approvals/{'0' * 32}/decision"),
    ],
)
def test_phase2_routes_require_the_token(client: TestClient, method: str, path: str) -> None:
    assert client.request(method, path, json={"decision": "deny"}).status_code == 401
    assert client.request(method, path, headers={"Authorization": "Bearer wrong"}, json={"decision": "deny"}).status_code == 401


# --- Server-sent events --------------------------------------------------------------


def _parse_sse(body: str) -> list[dict[str, Any]]:
    events = []
    for block in body.strip().split("\n\n"):
        fields: dict[str, Any] = {}
        for line in block.splitlines():
            if line.startswith(":"):
                fields.setdefault("comments", []).append(line)
                continue
            key, _, value = line.partition(": ")
            fields[key] = value
        events.append(fields)
    return events


def test_sse_streams_a_finished_run_and_closes(client: TestClient, auth: dict[str, str]) -> None:
    run_id = _new_run(client, auth)
    client.post(f"{API}/runs/{run_id}/start", headers=auth)
    _wait(client, auth, run_id, _settled)
    client.post(f"{API}/runs/{run_id}/cancel", headers=auth)  # terminal: the stream ends by itself

    response = client.get(f"{API}/runs/{run_id}/events/stream", headers=auth)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    events = _parse_sse(response.text)
    assert events[0] == {"retry": "3000"}
    assert events[-1]["event"] == "stream.end"
    data_events = [e for e in events if "id" in e]
    seqs = [int(e["id"]) for e in data_events]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))
    types = [e["event"] for e in data_events]
    assert types[0] == "run.created" and types[-1] == "run.cancelled"
    assert {"run.started", "llm.request", "tool.called", "tool.result", "plan.accepted", "run.paused"} <= set(types)
    for e in data_events:
        assert json.loads(e["data"])["seq"] == int(e["id"])
    assert API_TOKEN not in response.text


def test_sse_reconnect_with_last_event_id(client: TestClient, auth: dict[str, str]) -> None:
    run_id = _new_run(client, auth)
    client.post(f"{API}/runs/{run_id}/cancel", headers=auth)
    full = [e for e in _parse_sse(client.get(f"{API}/runs/{run_id}/events/stream", headers=auth).text) if "id" in e]
    last_seen = full[0]["id"]
    resumed = client.get(f"{API}/runs/{run_id}/events/stream", headers={**auth, "Last-Event-ID": last_seen}).text
    resumed_ids = [e["id"] for e in _parse_sse(resumed) if "id" in e]
    assert resumed_ids == [e["id"] for e in full[1:]]
    via_query = client.get(f"{API}/runs/{run_id}/events/stream?after_seq={last_seen}", headers=auth).text
    assert [e["id"] for e in _parse_sse(via_query) if "id" in e] == resumed_ids


def test_sse_on_an_active_run_closes_at_the_time_limit(tmp_path: Path, auth: dict[str, str]) -> None:
    settings = make_settings(tmp_path, sse_poll_seconds=0.02, sse_heartbeat_seconds=0.05, sse_max_stream_seconds=0.3)
    with TestClient(create_app(settings, llm=FakeLLM())) as client:
        run_id = _new_run(client, auth)  # CREATED, never started: the stream stays open until the limit
        started = time.monotonic()
        body = client.get(f"{API}/runs/{run_id}/events/stream", headers=auth).text
        assert time.monotonic() - started >= 0.3
    events = _parse_sse(body)
    assert [e["event"] for e in events if "event" in e] == ["run.created"]
    comments = [c for e in events for c in e.get("comments", [])]
    assert ": keepalive" in comments
    assert any("reconnect with Last-Event-ID" in c for c in comments)


def test_sse_unknown_run_is_404(client: TestClient, auth: dict[str, str]) -> None:
    assert client.get(f"{API}/runs/{'f' * 32}/events/stream", headers=auth).status_code == 404


def test_sse_rejects_token_in_query_string(client: TestClient, auth: dict[str, str]) -> None:
    run_id = _new_run(client, auth)
    response = client.get(f"{API}/runs/{run_id}/events/stream?access_token={API_TOKEN}")
    assert response.status_code == 401


# --- Demo ----------------------------------------------------------------------------


def test_offline_demo_runs_end_to_end(tmp_path: Path) -> None:
    lines: list[str] = []
    run = run_demo(lines.append, data_dir=tmp_path)
    output = "\n".join(lines)
    assert run["state"] == "IMPLEMENTING" and run["pause_reason"] == "phase_unavailable"
    assert "unknown stack 'django'" in output
    assert "2. GPT selected the stack: python-cli" in output
    assert "No calculator code was generated, executed or tested" in output
    assert "[REDACTED]" not in output


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c
