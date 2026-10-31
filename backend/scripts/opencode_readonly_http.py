"""Read-only compatibility check of the adapter's HTTP client against the real OpenCode 1.19.5 server.

This is not coding integration. No prompt is sent, no tool runs, no session is created, and no
model is reached. It shows that the adapter's real client reads the pinned server's real answers.

    & $env:DEVPILOT_PYTHON backend\\scripts\\opencode_readonly_http.py          # dry run: starts nothing
    & $env:DEVPILOT_PYTHON backend\\scripts\\opencode_readonly_http.py --run    # APPROVAL: starts the server

The server runs in the same guarded check container as the baseline: no network, no published port,
read-only root, no capabilities, user 65534, the same limits, the verified image and binary, a fresh
empty workspace and a fresh random password. Nothing on the host can reach it, so `RelayTransport`
carries each request through `docker exec -i` into a small relay inside the container. The adapter's
`OpenCodeClient` runs unchanged on top of it: real requests, real answers, no mock.

It cannot change anything, by construction, in two independent layers:
1. `RelayTransport` refuses, on the host, every method but GET and every path outside `READ_ONLY_PATHS`.
2. `http_relay.py` refuses the same, inside the container, before it opens a connection.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import secrets
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import opencode_contract_check as runner  # noqa: E402

from app.coding.opencode_check import CHECK_USER, CHECKS_TARGET, safe_console  # noqa: E402
from app.coding.opencode_contract import CONTRACT_VERSION  # noqa: E402
from app.coding.opencode_policy import verify_effective_config  # noqa: E402
from app.coding.opencode_tools import PINNED_TOOL_IDS, tool_inventory_problems  # noqa: E402
from app.coding.session_control import AttemptGuard, AttemptRecord  # noqa: E402
from app.coding.types import CodingTask  # noqa: E402
from app.tools.adapters.opencode import OpenCodeClient, OpenCodeError, OpenCodeNotFound, OpenCodeProvider, verify_contract  # noqa: E402

RELAY = Path(__file__).resolve().parent / "opencode_check" / "http_relay.py"
READ_ONLY_PATHS = frozenset(
    {"/global/health", "/doc", "/config", "/agent", "/session", "/session/status", "/experimental/tool/ids", "/event",
     "/devpilot/health", "/devpilot-readonly-check/unknown"}
)  # fmt: skip
_SESSION_PATH = re.compile(r"^/session/ses_[A-Za-z0-9]{1,64}(/message)?$")
UNKNOWN_SESSION = "ses_devpilotreadonlyunknown00"
PLACEHOLDER_URL = "http://opencode.in-container.invalid"
EXEC_SECONDS = 30.0
STARTUP_SECONDS = 60.0


class ReadOnlyViolation(RuntimeError):
    """Something tried to change the server. It was refused on the host and sent nowhere."""


def read_only_refusal(method: str, path: str) -> str | None:
    if method != "GET":
        return f"{method} is refused: this check is read-only"
    if path not in READ_ONLY_PATHS and not _SESSION_PATH.fullmatch(path):
        return f"GET {path} is outside this check's allowlist"
    return None


class RelayTransport(httpx.BaseTransport):
    """Carries one GET into the container and back. `relay` takes a JSON request and returns the relay's JSON."""

    def __init__(self, relay: Callable[[bytes], dict[str, Any]]) -> None:
        self._relay = relay
        self.forwarded: list[str] = []
        self.refused: list[str] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        reason = read_only_refusal(request.method, path)
        if reason is not None:
            self.refused.append(f"{request.method} {path}")
            raise ReadOnlyViolation(reason)
        self.forwarded.append(f"GET {path}")
        headers = {name: request.headers[name] for name in ("authorization", "accept") if name in request.headers}
        query = request.url.query.decode()
        answer = self._relay(json.dumps({"method": "GET", "path": path, "query": query, "headers": headers}).encode())
        if "refused" in answer:
            raise ReadOnlyViolation(f"the relay refused it: {answer['refused']}")
        if "status" not in answer:
            raise httpx.ConnectError(f"the relay could not reach the server: {answer.get('error')}", request=request)
        body = base64.b64decode(answer["body_b64"])
        return httpx.Response(answer["status"], headers=answer["headers"], stream=httpx.ByteStream(body), request=request)


class _MemoryStore:
    """Holds the one attempt of the refusal step. Nothing here is kept."""

    def claim(self, record: AttemptRecord) -> None: ...

    def save(self, record: AttemptRecord) -> None: ...

    def unresolved(self) -> list[AttemptRecord]:
        return []


class _NoWriter:
    def terminate(self) -> bool:
        raise ReadOnlyViolation("nothing in this check may reach the stop path")


def schema_problems(value: Any, schema: Any, components: dict[str, Any], where: str = "$", nulls: list[str] | None = None) -> list[str]:
    """Where a value leaves the server's own OpenAPI schema. Type, enum, const, required, items, anyOf, $ref.

    The real 1.19.5 server sends `null` for optional fields its schema types as boolean, number or
    string. Those are noted in `nulls` by path, not treated as problems: absent and null mean the same
    to everything DevPilot reads. A null in a required field is still a problem."""
    if not isinstance(schema, dict):
        return []
    if "$ref" in schema:
        return schema_problems(value, components[schema["$ref"].rsplit("/", 1)[1]], components, where, nulls)
    for key in ("anyOf", "oneOf"):
        if key in schema:
            options = [schema_problems(value, option, components, where, nulls) for option in schema[key]]
            return [] if any(not found for found in options) else [f"{where}: matches none of its {len(options)} forms"]
    if "const" in schema and value != schema["const"]:
        return [f"{where}: expected {schema['const']!r}"]
    if "enum" in schema and value not in schema["enum"]:
        return [f"{where}: {value!r} is not one of the listed values"]
    kind = schema.get("type")
    checks: dict[str, Callable[[Any], bool]] = {
        "object": lambda v: isinstance(v, dict), "array": lambda v: isinstance(v, list), "string": lambda v: isinstance(v, str),
        "boolean": lambda v: isinstance(v, bool), "number": lambda v: isinstance(v, int | float) and not isinstance(v, bool),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool), "null": lambda v: v is None,
    }  # fmt: skip
    kinds = kind if isinstance(kind, list) else [kind]  # OpenAPI 3.1 allows a list of types
    known = [k for k in kinds if k in checks]
    if known and not any(checks[k](value) for k in known):
        return [f"{where}: expected {' or '.join(known)}"]
    kind = next((k for k in known if checks[k](value)), None)
    problems: list[str] = []
    if kind == "object":
        problems += [f"{where}.{key}: required, missing" for key in schema.get("required", []) if key not in value]
        for key, item in schema.get("properties", {}).items():
            if key not in value:
                continue
            if value[key] is None and key not in schema.get("required", []) and nulls is not None:
                nulls.append(f"{where}.{key}")
                continue
            problems += schema_problems(value[key], item, components, f"{where}.{key}", nulls)
        extra = schema.get("additionalProperties")
        if isinstance(extra, dict):
            for key, item in value.items():
                if key not in schema.get("properties", {}):
                    problems += schema_problems(item, extra, components, f"{where}.{key}", nulls)
    if kind == "array":
        for index, item in enumerate(value):
            problems += schema_problems(item, schema.get("items"), components, f"{where}[{index}]", nulls)
    return problems


def declared_schema(spec: dict[str, Any], path: str) -> Any:
    return spec["paths"][path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]


class Results:
    def __init__(self) -> None:
        self.steps: list[dict[str, Any]] = []

    def record(self, name: str, passed: bool, detail: str) -> None:
        self.steps.append({"step": name, "result": "PASS" if passed else "FAIL", "detail": detail[:900]})

    def attempt(self, name: str, action: Callable[[], tuple[bool, str]]) -> None:
        """A step that raises is a failed step, never a crash: the container must still be cleaned up."""
        try:
            passed, detail = action()
        except Exception as exc:  # the step's own exception is the finding
            passed, detail = False, f"{type(exc).__name__}: {exc}"
        self.record(name, passed, detail)

    def refused(self, name: str, action: Callable[[], Any]) -> None:
        try:
            action()
        except ReadOnlyViolation as exc:
            self.record(name, True, f"refused, nothing sent: {exc}")
        except Exception as exc:  # anything else means the refusal did not happen as designed
            self.record(name, False, f"{type(exc).__name__}: {exc}")
        else:
            self.record(name, False, "it was not refused")


def _validated(client: OpenCodeClient, spec: dict[str, Any], path: str, read: Callable[[], Any]) -> tuple[bool, str]:
    value = read()
    nulls: list[str] = []
    problems = schema_problems(value, declared_schema(spec, path), spec.get("components", {}).get("schemas", {}), nulls=nulls)
    if problems:
        return False, "; ".join(problems[:5])
    note = f", with null in {len(nulls)} optional fields: {', '.join(nulls[:8])}" if nulls else ""
    return True, f"matches the server's own schema{note}"


def exercise(container: str, password: str) -> dict[str, Any]:
    """Everything that talks to the server. Runs while the container is up; it may only read."""

    def relay(payload: bytes) -> dict[str, Any]:
        args = ["exec", "-i", "--user", CHECK_USER, container, "python3", "-I", f"{CHECKS_TARGET}/http_relay.py"]
        done = subprocess.run([runner.docker_path(), *args], input=payload, capture_output=True, timeout=EXEC_SECONDS, check=False)
        try:
            return json.loads(done.stdout)
        except ValueError:
            return {"error": f"relay exit {done.returncode}"}

    transport = RelayTransport(relay)
    results = Results()
    client = OpenCodeClient(PLACEHOLDER_URL, username="opencode", password=password, directory="/workspace", transport=transport)
    try:
        read_everything(client, transport, results, relay)
    except Exception as exc:  # what was seen so far is kept, and the container is removed either way
        results.record("the check stopped early", False, f"{type(exc).__name__}: {exc}")
    finally:
        client.close()
    return {"steps": results.steps, "forwarded": transport.forwarded, "refused": transport.refused}


def read_everything(client: OpenCodeClient, transport: RelayTransport, results: Results, relay: Callable[[bytes], dict[str, Any]]) -> None:
    began = time.monotonic()
    version = None
    last = "not tried"
    while version is None and time.monotonic() - began < STARTUP_SECONDS:
        try:
            version = client.health()
        except OpenCodeError as exc:
            last = exc.message
            time.sleep(0.5)
    results.record("server answers, GET /global/health", version == CONTRACT_VERSION, f"version {version}, {time.monotonic() - began:.1f} s; last error: {last}")
    if version is None:
        return

    spec = client.spec()
    results.record("GET /doc", bool(spec.get("paths")), f"JSON OpenAPI {spec.get('openapi')}, {len(spec['paths'])} paths")
    contract = verify_contract(client)
    results.record("the adapter's contract check", contract.version == CONTRACT_VERSION and contract.client_message_id,
                   f"reply via {contract.permission_reply}; question reject {contract.question_reject}; client messageID {contract.client_message_id}")  # fmt: skip

    health = client._json("GET", "/global/health", scoped=False)
    results.attempt("schema of GET /global/health", lambda: _validated(client, spec, "/global/health", lambda: health))
    config, agents, tools, states = client.config(), client.agents(), client.tool_ids(), client.statuses()
    results.attempt("schema of GET /agent", lambda: _validated(client, spec, "/agent", lambda: agents))
    results.attempt("schema of GET /session/status", lambda: _validated(client, spec, "/session/status", lambda: states))
    results.attempt("schema of GET /experimental/tool/ids", lambda: _validated(client, spec, "/experimental/tool/ids", lambda: tools))
    results.attempt("schema of GET /config", lambda: _validated(client, spec, "/config", lambda: config))
    problems = verify_effective_config(config, agents)
    results.record("GET /config with GET /agent pass the effective-policy check", not problems, "; ".join(problems[:4]) or "no problems")
    problems = tool_inventory_problems(tools, contract.version, agents)
    results.record("the tool inventory matches the pinned list", not problems and set(tools) == PINNED_TOOL_IDS[CONTRACT_VERSION],
                   "; ".join(problems[:4]) or f"{len(tools)} tools, exactly the reviewed list")  # fmt: skip
    results.record("GET /session/status is empty", states == {}, json.dumps(states))

    def missing_session() -> tuple[bool, str]:
        return client.get_session(UNKNOWN_SESSION) is None, "404 reads as a missing session"

    def missing_messages() -> tuple[bool, str]:
        try:
            client.messages(UNKNOWN_SESSION)
        except OpenCodeNotFound as exc:
            return True, exc.message
        return False, "an unknown session answered with messages"

    def event_stream() -> tuple[bool, str]:
        with client.events(read_timeout=5.0) as stream:
            first = next(stream)
        return first.get("type") == "server.connected", f"first event {first.get('type')!r}"

    def html_page() -> tuple[bool, str]:
        try:
            client.filter_health()
        except OpenCodeError as exc:
            return "text/html" in exc.message, f"{exc.status}: {exc.message}"
        return False, "an HTML page was accepted as the filter"

    def unknown_path() -> tuple[bool, str]:
        try:
            client._json("GET", "/devpilot-readonly-check/unknown", scoped=False)
        except OpenCodeError as exc:
            return "text/html" in exc.message, f"{exc.status}: {exc.message}"
        return False, "an unknown path was accepted as JSON"

    def wrong_password() -> tuple[bool, str]:
        stranger = OpenCodeClient(PLACEHOLDER_URL, username="opencode", password=secrets.token_urlsafe(16), directory="/workspace", transport=transport)
        try:
            stranger.health()
        except OpenCodeError as exc:
            return str(exc.status) == "AUTH", f"{exc.status}: {exc.message}"
        finally:
            stranger.close()
        return False, "a wrong password was accepted"

    def no_sessions() -> tuple[bool, str]:
        listed = client._json("GET", "/session")
        return listed == [], f"{len(listed)} sessions exist"

    results.attempt("a missing session, GET /session/{id}", missing_session)
    results.attempt("a missing session's messages, GET /session/{id}/message", missing_messages)
    results.attempt("the event stream, GET /event", event_stream)
    results.attempt("HTTP 200 HTML is rejected, GET /devpilot/health", html_page)
    results.attempt("HTTP 200 HTML is rejected, an unknown path", unknown_path)
    results.attempt("a wrong password maps to AUTH", wrong_password)

    # Everything below must never reach the server.
    results.refused("POST /session refused", lambda: client.create_session("devpilot-readonly-check"))
    results.refused("POST prompt_async refused", lambda: client.prompt_async("ses_x", {"parts": []}))
    results.refused("POST abort refused", lambda: client.abort("ses_x"))
    results.refused("PATCH /config refused", lambda: client._http.request("PATCH", "/config", json={}))
    results.refused("PUT /auth refused", lambda: client._http.request("PUT", "/auth/openai", json={}))
    results.refused("DELETE /session refused", lambda: client._http.request("DELETE", "/session/ses_x"))
    results.refused("GET /pty refused", lambda: client._http.request("GET", "/pty"))

    def relay_refuses() -> tuple[bool, str]:
        answers = {
            method: relay(json.dumps({"method": method, "path": "/session"}).encode())
            for method in ("POST", "PATCH", "PUT", "DELETE")
        }
        passed = all("refused" in answer for answer in answers.values())
        return passed, "the relay refused POST, PATCH, PUT and DELETE before connecting" if passed else json.dumps(answers)[:300]

    results.attempt("the relay's own refusal, inside the container", relay_refuses)

    def provider_refuses() -> tuple[bool, str]:
        record = AttemptRecord(project_id="p", run_id="r", call_id="c", task_key="r:1")
        guard = AttemptGuard(record, store=_MemoryStore(), writer=_NoWriter(), fingerprint=lambda: "x")
        task = CodingTask(key="r:1", title="t", brief="unused", session_id=None, abort_first=False, max_tokens=1, timeout_seconds=1.0)
        outcome = OpenCodeProvider(client, model="openai/none").run_task(task, cancel=None, progress=lambda *_: None, guard=guard)
        return str(outcome.status) == "INCOMPATIBLE" and "no API filter" in (outcome.detail or ""), f"{outcome.status}: {(outcome.detail or '')[:160]}"

    results.attempt("the adapter refuses to drive a bare server", provider_refuses)
    results.attempt("still no session on the server", no_sessions)
    results.attempt("still nothing running", lambda: (client.statuses() == {}, "GET /session/status is empty"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run", action="store_true", help="start the real server (needs approval)")
    args = parser.parse_args()
    safe_console(sys.stdout)
    if not args.run:
        print("Read-only HTTP check: DRY RUN. Nothing is started.\n")
        print(f"Paths the host may forward, GET only: {', '.join(sorted(READ_ONLY_PATHS))}, and GET /session/ses_*[/message]")
        return 0 if runner.preflight_ok() else 1
    ready = runner.ready_binary()
    if ready is None:
        return 1
    binary_dir, binary_sha = ready
    runner.sweep()
    record = runner.one_run("readonly-http", binary_dir, {}, project_config_off=True, tmp_exec=False, guard=True, exercise=exercise)
    record["binary_sha256"] = binary_sha
    stamp = record.get("stamp") or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = runner.save_report(f"{stamp}-readonly-http.json", record)
    report = record.get("report") or {}
    steps = report.get("steps") or []
    for step in steps:
        print(f"  {step['result']:<5} {step['step']}: {step['detail']}")
    passed = sum(s["result"] == "PASS" for s in steps)
    forwarded = report.get("forwarded") or []
    print(f"\nTotals: {passed} PASS, {len(steps) - passed} FAIL of {len(steps)}")
    print(f"Requests that reached the relay: {len(forwarded)}, methods: {sorted({f.split()[0] for f in forwarded})}")
    print(f"Requests refused on the host: {len(report.get('refused') or [])}")
    print(f"Isolation problems: {record.get('isolation_problems')}  Workspace changes: {record.get('workspace_changes')}")
    print(f"Cleanup: {record.get('cleanup')}  Duration: {record.get('duration_seconds')} s\nReport: {path}")
    if "error" in record:
        print(f"STOPPED: {record['error']}")
    ok = bool(steps) and passed == len(steps) and not record.get("isolation_problems") and not record.get("workspace_changes")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
