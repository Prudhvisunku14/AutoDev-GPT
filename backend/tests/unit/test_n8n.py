"""The n8n integration: workflow registry, HMAC signing, the HTTP client, the runtime, the policy exception, GPT's tools
through the real orchestrator and approval queue, and the signed callback route.

n8n is simulated (FakeN8n, or httpx.MockTransport for the client). Nothing here opens a socket or reads the real .env."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from app.config import REPO_ROOT
from app.core.egress import EgressPolicy
from app.db.models import Approval, ApprovalStatus, RunState
from app.main import create_app
from app.n8n.client import Execution, HttpN8nClient, N8nRefused, N8nUnavailable, RemoteWorkflow, TriggerAnswer, check_base_url
from app.n8n.fixtures import FakeN8n
from app.n8n.runtime import CALLBACK_PATH, N8nRuntime, build_n8n_runtime
from app.n8n.signing import SIGNATURE_HEADER, TIMESTAMP_HEADER, ReplayGuard, SignatureRejected, sign, signed_headers, verify
from app.n8n.workflows import WorkflowCatalog, WorkflowConfigError
from app.orchestrator.fake_llm import FakeLLM, Step, call
from app.tools.adapters import n8n as n8n_tools
from app.tools.adapters.n8n import register_n8n_tools
from app.tools.base import RiskLevel, ToolArgs, ToolSpec
from app.tools.policy import Decision, PolicyEngine
from app.tools.registry import ToolRegistrationError, ToolRegistry
from tests.harness import Harness
from tests.helpers import ALLOWLIST_FILE, API_TOKEN, make_settings
from tests.unit.test_coding_tools import pause

SECRET = "test-n8n-secret-" + "s" * 32
API_KEY = "test-n8n-api-key-" + "k" * 24
CALLBACK = "http://127.0.0.1:8000" + CALLBACK_PATH
EGRESS = EgressPolicy.from_file(ALLOWLIST_FILE)
TRIGGER_HEX = "0123456789abcdef"
TRIGGER_ID = f"trg-{TRIGGER_HEX}"
HEADERS = {"Authorization": f"Bearer {API_TOKEN}"}
N8N = {"n8n_enabled": True, "n8n_webhook_secret": SECRET, "n8n_callback_url": CALLBACK, "n8n_api_key": API_KEY}


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "workflows.yaml"
    path.write_text(text, encoding="utf-8")
    return path


# --- The workflow registry -------------------------------------------------------------------------------------------


def test_the_shipped_workflow_file_registers_notify_and_echo() -> None:
    catalog = WorkflowCatalog.from_file(REPO_ROOT / "config" / "n8n_workflows.yaml")
    assert catalog.keys == ["echo", "notify"]
    notify, echo = catalog.get("notify"), catalog.get("echo")
    assert notify is not None and notify.inputs == ("title", "message") and not notify.callback
    assert echo is not None and echo.inputs == ("text",) and echo.callback and echo.path == "devpilot-echo"


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("workflows:\n  a:\n    path: ../admin\n    description: x\n", "one URL segment"),
        ("workflows:\n  a:\n    path: a/b\n    description: x\n", "one URL segment"),
        ("workflows:\n  a:\n    path: p\n    description: x\n  b:\n    path: p\n    description: y\n", "listed twice"),
        ("workflows:\n  a:\n    path: p\n    description: x\n    url: http://evil\n", "unknown fields"),
        ("workflows:\n  a:\n    path: p\n    description: x\n    inputs: [Bad-Name]\n", "inputs must be"),
        ("workflows:\n  a:\n    path: p\n    description: x\n    inputs: [a, a]\n", "listed twice"),
        ("workflows:\n  A_B:\n    path: p\n    description: x\n", "must match"),
        ("workflows:\n  a:\n    path: p\n    description: ''\n", "description"),
        ("workflows:\n  a:\n    path: p\n    description: x\n    callback: yes please\n", "callback"),
        ("workflows: [a, b]\n", "mapping"),
    ],
)
def test_a_malformed_workflow_file_is_refused(tmp_path: Path, body: str, message: str) -> None:
    with pytest.raises(WorkflowConfigError, match=message):
        WorkflowCatalog.from_file(write(tmp_path, body))


def test_a_missing_workflow_file_is_refused_and_an_empty_one_registers_nothing(tmp_path: Path) -> None:
    with pytest.raises(WorkflowConfigError, match="not found"):
        WorkflowCatalog.from_file(tmp_path / "absent.yaml")
    assert len(WorkflowCatalog.from_file(write(tmp_path, "workflows:\n"))) == 0


# --- Signing ---------------------------------------------------------------------------------------------------------


def test_a_signature_verifies_once_and_only_for_the_same_secret_body_and_time() -> None:
    body, now = b'{"a":1}', 1_800_000_000
    headers = signed_headers(SECRET, body, now=now)
    assert headers[SIGNATURE_HEADER] == sign(SECRET, now, body) and headers[SIGNATURE_HEADER].startswith("sha256=")
    guard = ReplayGuard()
    verify(SECRET, headers[TIMESTAMP_HEADER], headers[SIGNATURE_HEADER], body, guard=guard, now=now + 10)
    with pytest.raises(SignatureRejected, match="already received"):
        verify(SECRET, headers[TIMESTAMP_HEADER], headers[SIGNATURE_HEADER], body, guard=guard, now=now + 11)
    for secret, data in (("another-secret", body), (SECRET, b'{"a":2}')):
        with pytest.raises(SignatureRejected, match="does not match"):
            verify(secret, headers[TIMESTAMP_HEADER], headers[SIGNATURE_HEADER], data, guard=ReplayGuard(), now=now)


@pytest.mark.parametrize("skew", [301, -301, 10_000])
def test_a_signature_outside_the_five_minute_window_is_refused_on_either_side(skew: int) -> None:
    body, now = b"{}", 1_800_000_000
    headers = signed_headers(SECRET, body, now=now)
    with pytest.raises(SignatureRejected, match="five-minute window"):
        verify(SECRET, headers[TIMESTAMP_HEADER], headers[SIGNATURE_HEADER], body, guard=ReplayGuard(), now=now + skew)


@pytest.mark.parametrize(("timestamp", "signature", "message"), [(None, "sha256=x", "not signed"), ("1", None, "not signed"), ("-5", "sha256=x", "Unix time"), ("1e9", "sha256=x", "Unix time")])
def test_unsigned_or_malformed_headers_are_refused(timestamp: str | None, signature: str | None, message: str) -> None:
    with pytest.raises(SignatureRejected, match=message):
        verify(SECRET, timestamp, signature, b"{}", guard=ReplayGuard(), now=1)


def test_the_replay_guard_forgets_old_signatures_and_fails_closed_when_full() -> None:
    guard = ReplayGuard(window=10, max_entries=2)
    assert guard.first_use("a", 0) and guard.first_use("b", 1) and not guard.first_use("c", 2)  # full: refused, not forgotten
    assert guard.first_use("c", 25)  # a and b are older than twice the window
    assert not guard.first_use("c", 26)


# --- The HTTP client ---------------------------------------------------------------------------------------------------


def http(handler: Any, *, api_key: str | None = API_KEY, base: str = "http://127.0.0.1:5678", max_bytes: int = 64_000) -> HttpN8nClient:
    return HttpN8nClient(base, EGRESS, api_key=SecretStr(api_key) if api_key else None, client=httpx.Client(transport=httpx.MockTransport(handler)), max_bytes=max_bytes)


def test_a_trigger_posts_the_signed_body_to_the_webhook_path_only() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"message": "Workflow was started"})

    body = b'{"x":1}'
    answer = http(handler).trigger("devpilot-echo", body, signed_headers(SECRET, body))
    request = seen[0]
    assert answer.status_code == 200 and "Workflow was started" in answer.body
    assert request.method == "POST" and str(request.url) == "http://127.0.0.1:5678/webhook/devpilot-echo" and request.content == body
    assert request.headers[SIGNATURE_HEADER].startswith("sha256=") and "X-N8N-API-KEY" not in request.headers  # the API key is not sent to webhooks


def test_a_trigger_reports_a_missing_workflow_a_redirect_an_error_and_an_oversized_answer() -> None:
    cases = [(httpx.Response(404), N8nUnavailable, "no active workflow"), (httpx.Response(302, headers={"location": "http://evil.example"}), N8nRefused, "redirect"),
             (httpx.Response(500), N8nUnavailable, "answered 500"), (httpx.Response(200, content=b"x" * 5_000), N8nUnavailable, "byte limit")]  # fmt: skip
    for response, error, message in cases:
        with pytest.raises(error, match=message):
            http(lambda _r, response=response: response, max_bytes=1_024).trigger("p", b"{}", {})

    def broken(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(N8nUnavailable, match="could not be reached"):
        http(broken).trigger("p", b"{}", {})


def test_the_egress_allowlist_decides_which_n8n_can_be_reached() -> None:
    contacted: list[str] = []
    for base in ("http://127.0.0.1:9999", "http://10.0.0.5:5678", "https://n8n.example.com"):
        with pytest.raises(N8nRefused, match="egress policy refused"):
            http(lambda r: (contacted.append(str(r.url)), httpx.Response(200))[1], base=base).trigger("p", b"{}", {})
    assert contacted == []


def test_the_base_url_must_be_scheme_host_and_port_only() -> None:
    assert check_base_url("http://127.0.0.1:5678/") == "http://127.0.0.1:5678"
    for bad in ("http://127.0.0.1:5678/webhook", "ftp://127.0.0.1:5678", "http://u:p@127.0.0.1:5678", "http://127.0.0.1:5678?x=1", "127.0.0.1:5678"):
        with pytest.raises(ValueError):
            check_base_url(bad)


def test_the_public_api_pages_through_workflows_and_finds_their_webhook_paths() -> None:
    seen: list[httpx.Request] = []
    pages = {
        None: {"data": [{"id": "1", "name": "Echo", "active": True, "nodes": [{"type": "n8n-nodes-base.webhook", "parameters": {"path": "/devpilot-echo"}}]}], "nextCursor": "c2"},
        "c2": {"data": [{"id": "2", "name": "Other", "active": False, "nodes": [{"type": "n8n-nodes-base.set", "parameters": {"path": "not-a-webhook"}}]}], "nextCursor": None},
    }  # fmt: skip

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=pages[request.url.params.get("cursor")])

    found = http(handler).workflows()
    assert found == [RemoteWorkflow("1", "Echo", True, ("devpilot-echo",)), RemoteWorkflow("2", "Other", False, ())]
    assert all(r.headers["X-N8N-API-KEY"] == API_KEY and r.url.path == "/api/v1/workflows" for r in seen) and len(seen) == 2


def test_executions_are_read_with_their_status_including_older_n8n_versions() -> None:
    data = {"data": [{"id": 7, "status": "error", "mode": "webhook", "startedAt": "s", "stoppedAt": "t"}, {"id": 6, "finished": True}, {"id": 5, "finished": False}]}
    client = http(lambda r: httpx.Response(200, json=data) if r.url.params["workflowId"] == "wf1" else httpx.Response(404))
    assert [(e.id, e.status) for e in client.executions("wf1", limit=5)] == [("7", "error"), ("6", "success"), ("5", "unknown")]


def test_the_public_api_needs_the_key_and_reports_a_refused_one() -> None:
    with pytest.raises(N8nRefused, match="N8N_API_KEY is not set"):
        http(lambda _r: httpx.Response(200, json={"data": []}), api_key=None).workflows()
    with pytest.raises(N8nRefused, match="refused the API key"):
        http(lambda _r: httpx.Response(401)).workflows()
    with pytest.raises(N8nUnavailable, match="did not answer JSON"):
        http(lambda _r: httpx.Response(200, content=b"<html>")).workflows()


# --- The runtime and its health check ----------------------------------------------------------------------------------


def test_n8n_is_wired_only_when_enabled(tmp_path: Path) -> None:
    assert build_n8n_runtime(make_settings(tmp_path), EGRESS) is None
    runtime = build_n8n_runtime(make_settings(tmp_path, **N8N), EGRESS)
    assert isinstance(runtime, N8nRuntime) and isinstance(runtime.client, HttpN8nClient) and runtime.problems() == [] and not runtime.auto_approve


def test_the_runtime_names_every_configuration_problem(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, n8n_enabled=True, n8n_base_url="http://127.0.0.1:9999")
    problems = " | ".join(build_n8n_runtime(settings, EGRESS).problems())  # type: ignore[union-attr]
    assert "egress allowlist" in problems and "N8N_CALLBACK_URL is not set" in problems and "N8N_WEBHOOK_SECRET is not set" in problems
    with pytest.raises(ValueError, match="must end with"):
        build_n8n_runtime(make_settings(tmp_path, **{**N8N, "n8n_callback_url": "http://127.0.0.1:8000/elsewhere"}), EGRESS)
    with pytest.raises(WorkflowConfigError):
        build_n8n_runtime(make_settings(tmp_path, **N8N, n8n_workflows_file=tmp_path / "absent.yaml"), EGRESS)


def test_health_compares_each_registered_workflow_with_n8n(tmp_path: Path) -> None:
    fake = FakeN8n([RemoteWorkflow("wf1", "Echo", True, ("devpilot-echo",)), RemoteWorkflow("wf2", "Notify", False, ("devpilot-notify",))],
                   {"wf1": [Execution("9", "success", "webhook", "s", "t")]})  # fmt: skip
    runtime = build_n8n_runtime(make_settings(tmp_path, **N8N), EGRESS, client=fake)
    report = runtime.health()  # type: ignore[union-attr]
    by_key = {w["key"]: w for w in report["workflows"]}
    assert report["reachable"] and not report["ready"] and report["simulated"]
    assert by_key["echo"]["found"] and by_key["echo"]["active"] and by_key["echo"]["recent_executions"][0]["status"] == "success"
    assert by_key["notify"]["found"] and not by_key["notify"]["active"]
    fake.workflows_list[1] = RemoteWorkflow("wf2", "Notify", True, ("devpilot-notify",))
    assert runtime.health()["ready"]  # type: ignore[union-attr]
    fake.fail = N8nUnavailable("n8n could not be reached: ConnectError")
    down = runtime.health()  # type: ignore[union-attr]
    assert not down["reachable"] and not down["ready"] and "ConnectError" in down["error"] and all(w["found"] is None for w in down["workflows"])


# --- The policy exception ------------------------------------------------------------------------------------------------


class _Args(ToolArgs):
    pass


def _spec(risk: RiskLevel, *, auto: bool) -> ToolSpec:
    return ToolSpec(name="t", description="d", args_model=_Args, risk=risk, states=None, handler=lambda _c, _a: {}, auto_approved=auto)


def test_only_external_network_tools_can_skip_approval() -> None:
    for risk in (RiskLevel.GIT_PUSH, RiskLevel.DESTRUCTIVE, RiskLevel.READ, RiskLevel.WORKSPACE_WRITE):
        with pytest.raises(ToolRegistrationError, match="only EXTERNAL_NETWORK"):
            ToolRegistry().register(_spec(risk, auto=True))
    engine = PolicyEngine()
    assert engine.decide(_spec(RiskLevel.EXTERNAL_NETWORK, auto=False), RunState.ANALYZING).decision is Decision.REQUIRE_APPROVAL
    assert engine.decide(_spec(RiskLevel.EXTERNAL_NETWORK, auto=True), RunState.ANALYZING).decision is Decision.ALLOW
    assert engine.decide(_spec(RiskLevel.GIT_PUSH, auto=True), RunState.ANALYZING).decision is Decision.REQUIRE_APPROVAL  # even if built without the registry
    assert engine.decide(_spec(RiskLevel.EXTERNAL_NETWORK, auto=True), RunState.COMPLETED).decision is Decision.DENY


# --- GPT's tools, through the real orchestrator and approval queue ------------------------------------------------------


def trigger(workflow: str = "echo", **inputs: str) -> Step:
    return call("trigger_workflow", {"workflow": workflow, "inputs": [{"name": k, "value": v} for k, v in (inputs or {"text": "hello"}).items()]})


RESULT = call("get_workflow_result", {"trigger_id": TRIGGER_ID})


@pytest.fixture(autouse=True)
def fixed_trigger_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(n8n_tools.secrets, "token_hex", lambda _n: TRIGGER_HEX)


def rig(tmp_path: Path, *steps: Step, fake: FakeN8n | None = None, **overrides: Any) -> tuple[Harness, str, N8nRuntime, FakeN8n]:
    fake = fake or FakeN8n.with_active("devpilot-echo", "devpilot-notify")
    h = Harness(tmp_path, FakeLLM(*steps), **{**N8N, **overrides})
    runtime = build_n8n_runtime(h.settings, EGRESS, client=fake)
    assert runtime is not None
    register_n8n_tools(h.registry, runtime)
    return h, h.new_run("Notify me when the build finishes.", "Notifier"), runtime, fake


def output(h: Harness, run_id: str, tool: str) -> dict[str, Any]:
    return [json.loads(i.output_json) for i in h.invocations(run_id) if i.tool == tool and i.output_json][-1]


def pending_approval(h: Harness, run_id: str) -> Approval:
    with h.sessions() as s:
        return s.scalars(select(Approval).where(Approval.run_id == run_id, Approval.status == ApprovalStatus.PENDING)).one()


def test_a_trigger_waits_for_approval_and_sends_nothing_before_it(tmp_path: Path) -> None:
    h, run_id, _runtime, fake = rig(tmp_path, trigger(), pause())
    assert h.advance(run_id).state is RunState.AWAITING_APPROVAL and fake.triggered == []
    approval = pending_approval(h, run_id)
    details = json.loads(approval.details_json) if getattr(approval, "details_json", None) else {}
    assert approval.tool == "trigger_workflow" and approval.risk == "EXTERNAL_NETWORK"
    if details:  # the person sees what the workflow does and the exact inputs
        assert "Round-trip" in json.dumps(details) and "hello" in json.dumps(details)


def test_an_approved_trigger_posts_one_signed_body_with_the_run_and_inputs(tmp_path: Path) -> None:
    h, run_id, runtime, fake = rig(tmp_path, trigger(), pause())
    h.advance(run_id)
    h.decide(pending_approval(h, run_id).id, "approve")
    h.advance(run_id)
    [(path, body, headers)] = fake.triggered
    payload = json.loads(body)
    assert path == "devpilot-echo" and payload == {"trigger_id": TRIGGER_ID, "workflow": "echo", "project_id": h.run(run_id).project_id, "run_id": run_id,
                                                   "inputs": {"text": "hello"}, "callback_url": CALLBACK}  # fmt: skip
    verify(SECRET, headers[TIMESTAMP_HEADER], headers[SIGNATURE_HEADER], body, guard=ReplayGuard())  # n8n can check it with the shared secret
    data = output(h, run_id, "trigger_workflow")["data"]
    assert data["trigger_id"] == TRIGGER_ID and data["accepted"] and data["callback_expected"] and data["simulated"] and "untrusted" in data["note"]
    assert [e["trigger_id"] for e in h.events_of(run_id, "n8n.trigger_started")] == [TRIGGER_ID] and h.events_of(run_id, "n8n.triggered")[0]["status_code"] == 200
    shown = json.dumps([h.events_of(run_id, t) for t in ("n8n.trigger_started", "n8n.triggered")]) + "".join(i.output_json or "" for i in h.invocations(run_id))
    assert SECRET not in shown and runtime.secret == SECRET


def test_a_denied_trigger_sends_nothing(tmp_path: Path) -> None:
    h, run_id, _runtime, fake = rig(tmp_path, trigger(), pause())
    h.advance(run_id)
    h.decide(pending_approval(h, run_id).id, "deny")
    h.advance(run_id)
    assert fake.triggered == [] and output(h, run_id, "trigger_workflow")["error"]["class"] == "APPROVAL_DENIED"


@pytest.mark.parametrize(
    ("step", "error", "message"),
    [
        (trigger("deploy-prod", text="x"), "POLICY_REFUSED", "not a registered workflow"),
        (trigger("echo", text="x", extra="y"), "VALIDATION_FAILED", "exactly these inputs"),
        (trigger("notify", title="only a title"), "VALIDATION_FAILED", "exactly these inputs"),
        (call("trigger_workflow", {"workflow": "echo", "inputs": [{"name": "text", "value": "a"}, {"name": "text", "value": "b"}]}), "VALIDATION_FAILED", "twice"),
    ],
)
def test_an_unregistered_workflow_or_wrong_inputs_are_refused_without_sending(tmp_path: Path, step: Step, error: str, message: str) -> None:
    h, run_id, _runtime, fake = rig(tmp_path, step, pause(), n8n_auto_approve=True)
    h.advance(run_id)
    result = output(h, run_id, "trigger_workflow")
    assert fake.triggered == [] and result["error"]["class"] == error and message in result["error"]["summary"]
    assert h.events_of(run_id, "n8n.trigger_started") == []  # a refused call leaves no trigger behind


def test_a_misconfigured_n8n_refuses_every_trigger(tmp_path: Path) -> None:
    h, run_id, _runtime, fake = rig(tmp_path, trigger(), pause(), n8n_auto_approve=True, n8n_webhook_secret=None)
    h.advance(run_id)
    result = output(h, run_id, "trigger_workflow")
    assert fake.triggered == [] and result["error"]["class"] == "POLICY_REFUSED" and "N8N_WEBHOOK_SECRET" in result["error"]["summary"]


def test_auto_approve_triggers_without_asking_and_an_outage_is_reported(tmp_path: Path) -> None:
    h, run_id, _runtime, fake = rig(tmp_path, trigger("notify", title="Build", message="done"), pause(), n8n_auto_approve=True)
    h.advance(run_id)
    with h.sessions() as s:
        assert s.scalars(select(Approval).where(Approval.run_id == run_id)).all() == []
    assert [p for p, _b, _h in fake.triggered] == ["devpilot-notify"] and output(h, run_id, "trigger_workflow")["data"]["callback_expected"] is False

    h2, run2, _r2, down = rig(tmp_path / "down", trigger(), pause(), n8n_auto_approve=True, fake=FakeN8n(fail=N8nUnavailable("n8n could not be reached: ConnectError")))
    h2.advance(run2)
    result = output(h2, run2, "trigger_workflow")
    assert result["error"]["class"] == "TOOL_ERROR" and "ConnectError" in result["error"]["summary"] and h2.events_of(run2, "n8n.triggered") == []


def test_a_result_is_pending_until_n8n_reports_and_a_foreign_trigger_is_refused(tmp_path: Path) -> None:
    other = call("get_workflow_result", {"trigger_id": "trg-ffffffffffffffff"})
    h, run_id, _runtime, _fake = rig(tmp_path, trigger(), RESULT, other, pause(), n8n_auto_approve=True)
    h.advance(run_id)
    pending, refused = [json.loads(i.output_json) for i in h.invocations(run_id) if i.tool == "get_workflow_result" and i.output_json]
    assert pending["data"]["status"] == "pending" and pending["data"]["workflow"] == "echo"
    assert refused["error"]["class"] == "POLICY_REFUSED" and "in this run" in refused["error"]["summary"]


# --- The round trip: trigger -> n8n -> signed callback -> result ---------------------------------------------------------


class EchoN8n(FakeN8n):
    """Behaves like the echo template: it checks DevPilot's signature, then posts the text back, signed, at once."""

    def __init__(self) -> None:
        super().__init__([RemoteWorkflow("wf1", "DevPilot echo", True, ("devpilot-echo",))])
        self.site: TestClient | None = None
        self.callback_status: int | None = None

    def trigger(self, path: str, body: bytes, headers: dict[str, str]) -> TriggerAnswer:
        answer = super().trigger(path, body, headers)
        verify(SECRET, headers[TIMESTAMP_HEADER], headers[SIGNATURE_HEADER], body, guard=ReplayGuard())
        sent = json.loads(body)
        reply = json.dumps({"trigger_id": sent["trigger_id"], "project_id": sent["project_id"], "run_id": sent["run_id"], "status": "success",
                            "execution_id": "4711", "result": {"echo": sent["inputs"]["text"]}}).encode()  # fmt: skip
        assert self.site is not None and sent["callback_url"].endswith(CALLBACK_PATH)
        self.callback_status = self.site.post(CALLBACK_PATH, content=reply, headers={**signed_headers(SECRET, reply), "Content-Type": "application/json"}).status_code
        return answer


def test_a_workflow_result_travels_back_through_the_signed_callback(tmp_path: Path) -> None:
    echo = EchoN8n()
    h, run_id, runtime, _fake = rig(tmp_path, trigger(text="ping"), RESULT, pause(), fake=echo, n8n_auto_approve=True)
    app = create_app(h.settings, llm=FakeLLM(), n8n_runtime=runtime)  # the same database as the orchestrator
    echo.site = TestClient(app, base_url="http://127.0.0.1:8000")
    try:
        h.advance(run_id)
    finally:
        app.state.engine.dispose()
    assert echo.callback_status == 200
    data = output(h, run_id, "get_workflow_result")["data"]
    assert data["status"] == "success" and data["execution_id"] == "4711" and json.loads(data["result"]) == {"echo": "ping"} and "untrusted" in data["note"]
    [stored] = h.events_of(run_id, "n8n.callback")
    assert stored["workflow"] == "echo" and stored["truncated"] is False


# --- The shipped n8n templates -------------------------------------------------------------------------------------------

TEMPLATES = REPO_ROOT / "n8n" / "workflows"
_NODE_HOST = """
const fs = require('fs');
const [codeFile, inputFile] = process.argv.slice(2);
const input = JSON.parse(fs.readFileSync(inputFile, 'utf8'));
globalThis.$env = { DEVPILOT_WEBHOOK_SECRET: input.secret };
globalThis.$execution = { id: 4711 };
globalThis.$input = { first: () => ({ json: { headers: input.headers, body: input.body } }) };
const run = new Function('require', 'return (async () => {' + fs.readFileSync(codeFile, 'utf8') + '\\n})();');
run(require).then((out) => process.stdout.write(JSON.stringify(out)), (err) => { process.stdout.write(JSON.stringify({ error: err.message })); });
"""


def _code_of(template: str) -> str:
    workflow = json.loads((TEMPLATES / template).read_text(encoding="utf-8"))
    return next(n["parameters"]["jsCode"] for n in workflow["nodes"] if n["type"] == "n8n-nodes-base.code")


def _run_template_code(tmp_path: Path, template: str, headers: dict[str, str], body: dict[str, Any]) -> Any:
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    (tmp_path / "host.js").write_text(_NODE_HOST, encoding="utf-8")
    (tmp_path / "code.js").write_text(_code_of(template), encoding="utf-8")
    (tmp_path / "input.json").write_text(json.dumps({"secret": SECRET, "headers": {k.lower(): v for k, v in headers.items()}, "body": body}), encoding="utf-8")
    done = subprocess.run([node, str(tmp_path / "host.js"), str(tmp_path / "code.js"), str(tmp_path / "input.json")], capture_output=True, text=True, timeout=30, check=False)
    return json.loads(done.stdout)


def test_the_templates_are_registered_and_listen_on_the_registered_paths() -> None:
    catalog = WorkflowCatalog.from_file(REPO_ROOT / "config" / "n8n_workflows.yaml")
    for spec in catalog:
        from app.n8n.client import parse_workflow

        workflow = parse_workflow(json.loads((TEMPLATES / f"{spec.path}.json").read_text(encoding="utf-8")))
        assert workflow.webhook_paths == (spec.path,)


def test_the_echo_templates_signature_code_agrees_with_devpilot_byte_for_byte(tmp_path: Path) -> None:
    """The template's JavaScript, run in Node: it accepts DevPilot's signature, refuses a forged one, and its reply passes
    DevPilot's own verification. This checks the template's code, not n8n itself."""
    body = {"trigger_id": TRIGGER_ID, "workflow": "echo", "project_id": "a" * 32, "run_id": "b" * 32, "inputs": {"text": "héllo \"quoted\" ✓"}, "callback_url": CALLBACK}
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")  # exactly what trigger_workflow sends
    out = _run_template_code(tmp_path, "devpilot-echo.json", signed_headers(SECRET, raw), body)
    assert isinstance(out, list) and out[0]["json"]["callback_url"] == CALLBACK
    reply = out[0]["json"]
    verify(SECRET, reply["timestamp"], reply["signature"], reply["body"].encode("utf-8"), guard=ReplayGuard())
    assert json.loads(reply["body"]) == {"trigger_id": TRIGGER_ID, "project_id": "a" * 32, "run_id": "b" * 32, "status": "success", "execution_id": "4711", "result": {"echo": body["inputs"]["text"]}}
    forged = _run_template_code(tmp_path, "devpilot-echo.json", signed_headers("not-the-secret", raw), body)
    stale = _run_template_code(tmp_path, "devpilot-notify.json", signed_headers(SECRET, raw, now=1_000_000_000), body)
    assert forged == {"error": "DevPilot signature rejected"} and stale == {"error": "DevPilot signature rejected"}


# --- The callback route -------------------------------------------------------------------------------------------------


@pytest.fixture
def site(tmp_path: Path):  # noqa: ANN201
    made: list[Any] = []

    def build(*, triggered: bool = True, callback: bool = True, fake: FakeN8n | None = None, **overrides: Any) -> tuple[TestClient, str, str]:
        settings = make_settings(tmp_path / str(len(made)), log_level="ERROR", **{**N8N, **overrides})
        runtime = build_n8n_runtime(settings, EGRESS, client=fake or FakeN8n.with_active("devpilot-echo", "devpilot-notify"))
        app = create_app(settings, llm=FakeLLM(), n8n_runtime=runtime)
        made.append(app)
        client = TestClient(app, base_url="http://127.0.0.1:8000")
        project = client.post("/api/v1/projects", json={"name": "p", "requirement_text": "Notify me."}, headers=HEADERS).json()["id"]
        run = client.post(f"/api/v1/projects/{project}/runs", headers=HEADERS).json()["id"]
        if triggered:
            with app.state.session_factory.begin() as s:
                app.state.events.record(s, project_id=project, run_id=run, type="n8n.trigger_started", payload={"trigger_id": TRIGGER_ID, "workflow": "echo", "callback": callback})
        return client, project, run

    yield build
    for app in made:
        app.state.engine.dispose()


def reply(project: str, run: str, **changes: Any) -> bytes:
    return json.dumps({"trigger_id": TRIGGER_ID, "project_id": project, "run_id": run, "status": "success", "execution_id": "1", "result": {"ok": True}, **changes}).encode()


def post(client: TestClient, body: bytes, *, secret: str = SECRET, now: float | None = None, headers: dict[str, str] | None = None) -> httpx.Response:
    return client.post(CALLBACK_PATH, content=body, headers=headers if headers is not None else {**signed_headers(secret, body, now=now), "Content-Type": "application/json"})


def test_a_signed_callback_is_stored_once(site: Any) -> None:
    client, project, run = site()
    body = reply(project, run)
    assert post(client, body).json() == {"accepted": True, "trigger_id": TRIGGER_ID}
    again = post(client, reply(project, run, execution_id="2"))  # a new signature for a second report of the same trigger
    assert again.status_code == 409 and "already has its callback" in again.json()["detail"]
    with client.app.state.session_factory() as s:  # type: ignore[attr-defined]
        from app.core.events import event_payload, list_events

        stored = [event_payload(e) for e in list_events(s, run_id=run, limit=100) if e.type == "n8n.callback"]
    assert len(stored) == 1 and stored[0]["status"] == "success" and json.loads(stored[0]["result"]) == {"ok": True}


def test_an_unsigned_wrongly_signed_stale_or_replayed_callback_is_refused(site: Any) -> None:
    import time

    client, project, run = site()
    body = reply(project, run)
    assert post(client, body, headers={"Content-Type": "application/json"}).status_code == 401
    assert post(client, body, secret="not-the-secret").status_code == 401
    assert post(client, body, now=time.time() - 600).status_code == 401
    headers = {**signed_headers(SECRET, body), "Content-Type": "application/json"}
    assert post(client, body, headers=headers).status_code == 200
    replayed = post(client, body, headers=headers)
    assert replayed.status_code == 401 and "already received" in replayed.json()["detail"]


def test_a_callback_must_name_a_trigger_this_run_made_for_a_workflow_that_reports_back(site: Any) -> None:
    client, project, run = site(triggered=False)
    assert post(client, reply(project, run)).status_code == 404
    assert post(client, reply("f" * 32, run)).status_code == 404  # the run is not in that project
    client2, project2, run2 = site(callback=False)
    assert post(client2, reply(project2, run2)).status_code == 409


def test_a_malformed_or_oversized_callback_is_refused_and_a_large_result_is_cut(site: Any) -> None:
    client, project, run = site()
    for bad in (b"not json", reply(project, run, status="maybe"), reply(project, run, extra="field"), reply(project, run, trigger_id="trg-x")):
        assert post(client, bad).status_code == 422
    assert post(client, b"{" + b" " * 70_000 + b"}").status_code == 413
    assert post(client, reply(project, run, result="r" * 20_000)).status_code == 200
    with client.app.state.session_factory() as s:  # type: ignore[attr-defined]
        from app.core.events import event_payload, list_events

        [stored] = [event_payload(e) for e in list_events(s, run_id=run, limit=100) if e.type == "n8n.callback"]
    assert stored["truncated"] is True and len(stored["result"]) == 8_000


def test_without_a_secret_no_callback_is_accepted(site: Any) -> None:
    client, project, run = site(n8n_webhook_secret=None)
    assert post(client, reply(project, run)).status_code == 503


def test_the_routes_do_not_exist_while_n8n_is_off(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path, log_level="ERROR"), llm=FakeLLM())
    try:
        client = TestClient(app, base_url="http://127.0.0.1:8000")
        assert client.post(CALLBACK_PATH, content=b"{}").status_code == 404
        assert client.get("/api/v1/integrations/n8n/status", headers=HEADERS).status_code == 404
        assert "trigger_workflow" not in app.state.registry.names
    finally:
        app.state.engine.dispose()


def test_the_status_route_needs_the_token_and_reports_health(site: Any) -> None:
    client, _project, _run = site(fake=FakeN8n.with_active("devpilot-echo"))
    assert client.get("/api/v1/integrations/n8n/status").status_code in (401, 403)
    report = client.get("/api/v1/integrations/n8n/status", headers=HEADERS).json()
    by_key = {w["key"]: w for w in report["workflows"]}
    assert report["reachable"] and not report["ready"] and by_key["echo"]["active"] and not by_key["notify"]["found"]
    assert SECRET not in json.dumps(report) and API_KEY not in json.dumps(report)
