"""OpenCode contract probe. It runs INSIDE the check container, never on the host.

It sends a fixed list of requests to the OpenCode server on the container's own loopback
(127.0.0.1:4096) and prints one JSON report of what it saw. It judges nothing: the host
analyses the report with DevPilot's tested checks (backend/app/coding/opencode_check.py).

Scope, enforced by ALLOWED: health, /doc, configuration, agents, tool ids, creating one empty
session and reading it, its status and messages, aborting it while idle, the event stream, and
one unknown path. It never sends a prompt or a command and never calls the shell, config-change,
auth, MCP, TUI or PTY endpoints. It never follows links found in responses. Standard library
only, run as `python3 -I`. The password comes from the container's environment and is never
written to the report.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import os
import re
import socket
import sys
import time
from typing import Any
from urllib.parse import quote, urlencode

HOST = "127.0.0.1"
PORT = 4096
WORKSPACE = "/workspace"
INPUT = "/checks/input.json"
UNKNOWN_PATH = "/devpilot-contract-check/unknown"
UNKNOWN_SESSION = "ses_devpilotunknown0000000000"
MAX_BODY = 8 * 1024 * 1024
TEXT_PREVIEW = 600
SSE_BYTES = 16 * 1024
SSE_SECONDS = 4.0

ALLOWED = frozenset(
    {
        ("GET", "/global/health"),
        ("GET", "/doc"),
        ("GET", "/config"),
        ("GET", "/agent"),
        ("GET", "/experimental/tool/ids"),
        ("GET", "/session/status"),
        ("POST", "/session"),
        ("GET", "/session/{id}"),
        ("GET", "/session/{id}/message"),
        ("POST", "/session/{id}/abort"),
        ("GET", "/event"),
        ("GET", UNKNOWN_PATH),
    }
)
_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_KEEP_HEADERS = ("content-type", "content-length", "transfer-encoding", "www-authenticate", "location", "cache-control")
_REF = re.compile(r"""(?:src|href|url)\s*[=:]\s*["']([^"'<>\s]{1,300})["']""", re.IGNORECASE)


class Refused(Exception):
    """A request outside ALLOWED: a bug in this probe, never sent."""


def target(method: str, template: str, ids: dict[str, str] | None = None, query: dict[str, str] | None = None) -> str:
    if (method, template) not in ALLOWED:
        raise Refused(f"{method} {template} is not in the probe's allowlist")
    path = template
    for key, value in (ids or {}).items():
        if not _ID.match(value):
            raise Refused(f"refusing the unsafe {key} {value!r}")
        path = path.replace("{" + key + "}", quote(value, safe=""))
    if "{" in path or "}" in path:
        raise Refused(f"unfilled path {template}")
    return path + ("?" + urlencode(query) if query else "")


def auth_header(mode: str) -> dict[str, str]:
    if mode == "none":
        return {}
    user = os.environ.get("OPENCODE_SERVER_USERNAME", "opencode")
    password = os.environ["OPENCODE_SERVER_PASSWORD"] if mode == "good" else "devpilot-wrong-password"
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


def _headers(response: http.client.HTTPResponse) -> dict[str, str]:
    return {name: value for name in _KEEP_HEADERS if (value := response.getheader(name)) is not None}


def call(
    method: str,
    template: str,
    *,
    auth: str = "good",
    ids: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
    body: Any = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    path = target(method, template, ids, query)
    record: dict[str, Any] = {"request": f"{method} {template}", "auth": auth}
    connection = http.client.HTTPConnection(HOST, PORT, timeout=timeout)
    started = time.monotonic()
    try:
        headers = {"Accept": "application/json, text/html;q=0.5, */*;q=0.1", **auth_header(auth)}
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        data = response.read(MAX_BODY + 1)
        record.update(status=response.status, headers=_headers(response))
        record.update(describe(data))
    except (OSError, http.client.HTTPException) as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        connection.close()
    record["ms"] = round((time.monotonic() - started) * 1000)
    return record


def describe(data: bytes) -> dict[str, Any]:
    out: dict[str, Any] = {"bytes": min(len(data), MAX_BODY), "truncated": len(data) > MAX_BODY}
    data = data[:MAX_BODY]
    out["sha256"] = hashlib.sha256(data).hexdigest()
    try:
        out["json"] = json.loads(data)
    except ValueError:
        text = data.decode("utf-8", "replace")
        out["text"] = text[:TEXT_PREVIEW]
        if "<" in text[:200]:
            out["html_refs"] = sorted(set(_REF.findall(text)))[:50]  # recorded, never followed
    return out


def stream(auth: str = "good") -> dict[str, Any]:
    """Open GET /event, keep the first complete event (or what arrives within SSE_SECONDS), close."""
    path = target("GET", "/event", query={"directory": WORKSPACE})
    record: dict[str, Any] = {"request": "GET /event", "auth": auth}
    connection = http.client.HTTPConnection(HOST, PORT, timeout=SSE_SECONDS)
    raw = bytearray()
    started = time.monotonic()
    try:
        connection.request("GET", path, headers={"Accept": "text/event-stream", **auth_header(auth)})
        response = connection.getresponse()
        record.update(status=response.status, headers=_headers(response))
        while len(raw) < SSE_BYTES and time.monotonic() - started < SSE_SECONDS:
            try:
                chunk = response.read1(4096)
            except (socket.timeout, TimeoutError):
                break
            if not chunk:
                break
            raw.extend(chunk)
            if b"\n\n" in raw or b"\r\n\r\n" in raw or b"\r\r" in raw:
                break
    except (OSError, http.client.HTTPException) as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        connection.close()
    record["seconds"] = round(time.monotonic() - started, 2)
    record["raw_b64"] = base64.b64encode(bytes(raw[:SSE_BYTES])).decode()
    record["preview"] = bytes(raw[:TEXT_PREVIEW]).decode("utf-8", "replace")
    return record


def wait_ready(seconds: float) -> dict[str, Any]:
    started = time.monotonic()
    attempts = 0
    last: Any = None
    while time.monotonic() - started < seconds:
        attempts += 1
        record = call("GET", "/global/health", timeout=2.0)
        if record.get("status") == 200:
            return {"ready": True, "seconds": round(time.monotonic() - started, 2), "attempts": attempts}
        last = record.get("error") or record.get("status")
        time.sleep(0.5)
    return {"ready": False, "seconds": round(time.monotonic() - started, 2), "attempts": attempts, "last": str(last)}


def session_id(record: dict[str, Any]) -> str | None:
    body = record.get("json")
    status = record.get("status") or 0
    if 200 <= status < 300 and isinstance(body, dict) and isinstance(body.get("id"), str) and _ID.match(body["id"]):
        return body["id"]
    return None


def run(wait: float, report: dict[str, Any]) -> None:
    """Fill `report` step by step, so a failure part-way still leaves what was seen."""
    scoped = {"directory": WORKSPACE}
    report.update(probe_version=1, startup=wait_ready(wait))
    if not report["startup"]["ready"]:
        return
    report["health"] = call("GET", "/global/health")
    report["auth"] = {
        "health_without_password": call("GET", "/global/health", auth="none"),
        "health_with_wrong_password": call("GET", "/global/health", auth="wrong"),
        "doc_without_password": call("GET", "/doc", auth="none"),
        "session_create_without_password": call(
            "POST", "/session", auth="none", query=scoped, body={"title": "devpilot-unauthenticated-probe"}
        ),
        "event_without_password": stream(auth="none"),
    }
    report["doc"] = call("GET", "/doc")
    report["config"] = call("GET", "/config", query=scoped)
    report["agents"] = call("GET", "/agent", query=scoped)
    report["tool_ids"] = call("GET", "/experimental/tool/ids", query=scoped)
    report["status_before"] = call("GET", "/session/status", query=scoped)

    with open(INPUT, encoding="utf-8") as handle:
        rules = json.load(handle)["session_rules"]
    created = call("POST", "/session", query=scoped, body={"title": "devpilot-contract-check", "permission": rules})
    report["session_create_with_rules"] = created
    sid = session_id(created)
    if sid is None:
        plain = call("POST", "/session", query=scoped, body={"title": "devpilot-contract-check-plain"})
        report["session_create_plain"] = plain
        sid = session_id(plain)
    if sid is not None:
        ids = {"id": sid}
        report["session_get"] = call("GET", "/session/{id}", ids=ids, query=scoped)
        report["session_status"] = call("GET", "/session/status", query=scoped)
        report["session_messages"] = call("GET", "/session/{id}/message", ids=ids, query=scoped)
        report["session_abort"] = call("POST", "/session/{id}/abort", ids=ids, query=scoped)
        report["session_status_after_abort"] = call("GET", "/session/status", query=scoped)
    report["session_unknown"] = call("GET", "/session/{id}", ids={"id": UNKNOWN_SESSION}, query=scoped)
    report["event"] = stream()
    report["unknown_path"] = call("GET", UNKNOWN_PATH)


def main() -> int:
    parser = argparse.ArgumentParser(description="OpenCode contract probe (runs inside the check container)")
    parser.add_argument("--wait", type=float, default=60.0, help="seconds to wait for the server to become healthy")
    args = parser.parse_args()
    report: dict[str, Any] = {}
    code = 0
    try:
        run(args.wait, report)
    except Refused as exc:
        report["probe_error"], code = f"refused: {exc}", 3
    except Exception as exc:  # reported as one line, never a traceback that could echo request data
        report["probe_error"], code = f"{type(exc).__name__}: {exc}", 4
    sys.stdout.write(json.dumps(report))
    sys.stdout.flush()
    return code


if __name__ == "__main__":
    sys.exit(main())
