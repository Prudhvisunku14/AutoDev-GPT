"""A08: a run that waits for an n8n workflow is paused, and the signed callback resumes it, but only under every rule.

End to end through the real app, run manager and callback route, with scripted GPT and a simulated n8n.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.core.events import event_payload, list_events
from app.db.models import Run, ToolInvocation
from app.main import create_app
from app.n8n.fixtures import FakeN8n
from app.n8n.runtime import CALLBACK_PATH, build_n8n_runtime
from app.n8n.signing import signed_headers
from app.orchestrator.fake_llm import FakeLLM, Step, call
from app.orchestrator.llm_client import LLMRequest, LLMResponse
from tests.helpers import API_TOKEN, make_settings
from tests.unit.test_coding_tools import pause
from tests.unit.test_n8n import EGRESS, N8N, SECRET

HEADERS = {"Authorization": f"Bearer {API_TOKEN}"}
TRIGGER = call("trigger_workflow", {"workflow": "echo", "inputs": [{"name": "text", "value": "hello"}]})


def _trigger_id(request: LLMRequest) -> str:
    for item in reversed(request.input):
        if item.get("type") == "function_call_output" and "trigger_id" in item.get("output", ""):
            return json.loads(item["output"])["data"]["trigger_id"]
    raise AssertionError("no trigger_id in the request")


def with_trigger(name: str) -> Step:
    def step(request: LLMRequest, n: int) -> LLMResponse:
        return call(name, {"trigger_id": _trigger_id(request)})(request, n)

    return step


def started(tmp_path: Path, *steps: Step, **overrides: Any) -> tuple[TestClient, str, str]:
    settings = make_settings(tmp_path, log_level="ERROR", **{**N8N, "openai_price_input_per_mtok": 1.0, "openai_price_output_per_mtok": 1.0, "n8n_auto_approve": True, "n8n_callback_url": "http://127.0.0.1:8000" + CALLBACK_PATH, **overrides})
    runtime = build_n8n_runtime(settings, EGRESS, client=FakeN8n.with_active("devpilot-echo", "devpilot-notify", "devpilot-run-events"))
    client = TestClient(create_app(settings, llm=FakeLLM(*steps), n8n_runtime=runtime), base_url="http://127.0.0.1:8000")
    client.__enter__()
    project = client.post("/api/v1/projects", headers=HEADERS, json={"name": "Notifier", "requirement_text": "Echo a message through n8n and report it."}).json()["id"]
    run = client.post(f"/api/v1/projects/{project}/runs", headers=HEADERS).json()["id"]
    limit = client.get("/api/v1/budget", headers=HEADERS).json()["run_limit_usd"]
    assert client.post(f"/api/v1/runs/{run}/start", headers=HEADERS, json={"approved_max_usd": limit}).status_code == 202
    return client, project, run


def wait_until(client: TestClient, run_id: str, check: Any, seconds: float = 10.0) -> dict[str, Any]:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        run = client.get(f"/api/v1/runs/{run_id}", headers=HEADERS).json()
        if check(run):
            return run
        time.sleep(0.05)
    raise AssertionError(f"run never reached the expected state: {run}")


def callback(client: TestClient, project: str, run: str, trigger_id: str) -> dict[str, Any]:
    body = json.dumps({"trigger_id": trigger_id, "project_id": project, "run_id": run, "status": "success", "execution_id": "7", "result": {"echo": "hello"}}).encode()
    return client.post(CALLBACK_PATH, content=body, headers={**signed_headers(SECRET, body), "Content-Type": "application/json"}).json()


def tools_called(client: TestClient, run_id: str) -> list[str]:
    with client.app.state.session_factory() as s:  # type: ignore[attr-defined]
        return [i.tool for i in s.query(ToolInvocation).filter(ToolInvocation.run_id == run_id).order_by(ToolInvocation.seq)]


def test_the_run_pauses_while_waiting_and_the_signed_callback_resumes_it(tmp_path: Path) -> None:
    client, project, run = started(tmp_path, TRIGGER, with_trigger("wait_for_workflow"), with_trigger("get_workflow_result"), pause())
    try:
        waiting = wait_until(client, run, lambda r: (r["pause_reason"] or "").startswith("n8n_callback:"))
        trigger_id = waiting["pause_reason"].split(":", 1)[1]
        assert "continues by itself" in waiting["status_detail"]
        assert tools_called(client, run) == ["trigger_workflow", "wait_for_workflow"]  # no GPT call is made while it waits
        answer = callback(client, project, run, trigger_id)
        assert answer == {"accepted": True, "trigger_id": trigger_id, "resumed": True}
        wait_until(client, run, lambda _r: "get_workflow_result" in tools_called(client, run))
        with client.app.state.session_factory() as s:  # type: ignore[attr-defined]
            result = next(json.loads(i.output_json) for i in s.query(ToolInvocation).filter(ToolInvocation.run_id == run, ToolInvocation.tool == "get_workflow_result"))
            kinds = [e.type for e in list_events(s, run_id=run, limit=500)]
        assert result["data"]["status"] == "success" and json.loads(result["data"]["result"]) == {"echo": "hello"}
        assert "n8n.waiting" in kinds and "n8n.resumed_run" in kinds and kinds.index("n8n.callback") < kinds.index("run.resumed")
    finally:
        client.__exit__(None, None, None)


def test_with_spend_approval_required_the_callback_is_stored_but_a_person_continues(tmp_path: Path) -> None:
    client, project, run = started(tmp_path, TRIGGER, with_trigger("wait_for_workflow"), pause(), require_spend_approval=True)
    try:
        waiting = wait_until(client, run, lambda r: (r["pause_reason"] or "").startswith("n8n_callback:"))
        trigger_id = waiting["pause_reason"].split(":", 1)[1]
        assert callback(client, project, run, trigger_id)["resumed"] is False
        still = client.get(f"/api/v1/runs/{run}", headers=HEADERS).json()
        assert still["pause_reason"] == f"n8n_callback:{trigger_id}" and "REQUIRE_SPEND_APPROVAL is true" in still["status_detail"]
        assert tools_called(client, run) == ["trigger_workflow", "wait_for_workflow"]
    finally:
        client.__exit__(None, None, None)


def test_a_late_callback_does_not_resume_the_run(tmp_path: Path) -> None:
    client, project, run = started(tmp_path, TRIGGER, with_trigger("wait_for_workflow"), pause())
    try:
        waiting = wait_until(client, run, lambda r: (r["pause_reason"] or "").startswith("n8n_callback:"))
        trigger_id = waiting["pause_reason"].split(":", 1)[1]
        with client.app.state.session_factory.begin() as s:  # type: ignore[attr-defined]
            from app.db.models import Event

            for event in s.query(Event).filter(Event.run_id == run, Event.type == "n8n.waiting"):
                payload = event_payload(event)
                event.payload_json = json.dumps({**payload, "wait_seconds": 0})
        time.sleep(0.01)
        assert callback(client, project, run, trigger_id)["resumed"] is False
        assert "after the wait's deadline" in client.get(f"/api/v1/runs/{run}", headers=HEADERS).json()["status_detail"]
    finally:
        client.__exit__(None, None, None)


def test_a_callback_for_a_run_not_waiting_for_it_changes_nothing(tmp_path: Path) -> None:
    client, project, run = started(tmp_path, TRIGGER, pause())
    try:
        wait_until(client, run, lambda r: r["pause_reason"] is not None)
        with client.app.state.session_factory() as s:  # type: ignore[attr-defined]
            trigger_id = next(event_payload(e)["trigger_id"] for e in list_events(s, run_id=run, limit=200) if e.type == "n8n.trigger_started")
            before = s.get(Run, run).pause_reason
        assert callback(client, project, run, trigger_id)["resumed"] is False
        assert client.get(f"/api/v1/runs/{run}", headers=HEADERS).json()["pause_reason"] == before
    finally:
        client.__exit__(None, None, None)


def test_wait_returns_at_once_when_the_result_is_already_here(tmp_path: Path) -> None:
    """n8n answered before GPT asked to wait: the result comes back in the same call, and the run never pauses."""
    from tests.unit.test_n8n import rig

    def after_callback(request: LLMRequest, n: int) -> LLMResponse:
        trigger_id = _trigger_id(request)
        with h.sessions.begin() as s:
            h.events.record(s, project_id=h.run(run_id).project_id, run_id=run_id, type="n8n.callback",
                            payload={"trigger_id": trigger_id, "workflow": "echo", "status": "success", "execution_id": "1", "result": "{}", "truncated": False})  # fmt: skip
        return call("wait_for_workflow", {"trigger_id": trigger_id})(request, n)

    h, run_id, _runtime, _fake = rig(tmp_path, TRIGGER, after_callback, pause(), n8n_auto_approve=True, n8n_callback_url="http://127.0.0.1:8000" + CALLBACK_PATH)
    h.advance(run_id)
    out = [json.loads(i.output_json) for i in h.invocations(run_id) if i.tool == "wait_for_workflow"][0]
    assert out["data"]["status"] == "success" and "n8n.waiting" not in h.event_types(run_id)
