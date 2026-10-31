"""Carries one read-only HTTP request to the OpenCode server. It runs INSIDE the check container.

The container has no network and no published port, so nothing on the host can reach the server.
The read-only check on the host (backend/scripts/opencode_readonly_http.py) starts this script once
per request through `docker exec -i`. It reads one JSON request on stdin, so the password is never
on a command line, and prints one JSON answer.

This script forwards only GET, and only the paths in ALLOWED. Anything else is refused here, before
a connection is opened, whatever the host asked for. The host refuses first; this is the second,
independent layer. Standard library only, run as `python3 -I`.
"""

from __future__ import annotations

import base64
import http.client
import json
import re
import socket
import sys
import time
from typing import Any

HOST = "127.0.0.1"
PORTS = frozenset({4096, 8080})  # the server, and the API filter in front of it
MAX_BODY = 8 * 1024 * 1024
STREAM_SECONDS = 3.0
STREAM_BYTES = 16 * 1024
ALLOWED = frozenset(
    {
        "/global/health",
        "/doc",
        "/config",
        "/agent",
        "/session",
        "/session/status",
        "/experimental/tool/ids",
        "/event",
        "/devpilot/health",  # the API filter's route: a bare server answers it with its web page
        "/devpilot-readonly-check/unknown",
    }
)
_SESSION_PATH = re.compile(r"^/session/ses_[A-Za-z0-9]{1,64}(/message)?$")
_FORWARDED_HEADERS = ("authorization", "accept")
_KEPT_HEADERS = ("content-type", "content-length", "www-authenticate", "location")


def refusal(request: Any) -> str | None:
    """Why this request must not be forwarded. None only for a GET of an allowed path."""
    if not isinstance(request, dict):
        return "the request is not an object"
    if request.get("method") != "GET":
        return f"only GET is forwarded, not {request.get('method')!r}"
    path = request.get("path")
    if not isinstance(path, str) or not (path in ALLOWED or _SESSION_PATH.fullmatch(path)):
        return f"the path {path!r} is not in the relay's allowlist"
    query = request.get("query", "")
    if not isinstance(query, str) or query not in ("", "directory=%2Fworkspace", "directory=/workspace"):
        return f"the query {query!r} is not allowed"
    return None


def forward(request: dict[str, Any], port: int) -> dict[str, Any]:
    target = request["path"] + (f"?{request['query']}" if request.get("query") else "")
    headers = {k: v for k, v in (request.get("headers") or {}).items() if k in _FORWARDED_HEADERS}
    stream = request["path"] == "/event"
    connection = http.client.HTTPConnection(HOST, port, timeout=STREAM_SECONDS if stream else 20.0)
    try:
        connection.request("GET", target, headers=headers)
        response = connection.getresponse()
        kept = {name: value for name in _KEPT_HEADERS if (value := response.getheader(name)) is not None}
        body = _stream(response) if stream and response.status == 200 else response.read(MAX_BODY)
    except (OSError, http.client.HTTPException) as exc:
        return {"error": type(exc).__name__}
    finally:
        connection.close()
    return {"status": response.status, "headers": kept, "body_b64": base64.b64encode(body).decode()}


def _stream(response: http.client.HTTPResponse) -> bytes:
    """What the event stream sends within a short time: it never ends by itself."""
    started = time.monotonic()
    raw = bytearray()
    while len(raw) < STREAM_BYTES and time.monotonic() - started < STREAM_SECONDS:
        try:
            chunk = response.read1(4096)
        except (socket.timeout, TimeoutError):
            break
        if not chunk:
            break
        raw.extend(chunk)
        if b"\n\n" in raw:
            break
    return bytes(raw)


def main() -> int:
    try:
        request = json.load(sys.stdin)
    except ValueError:
        request = None
    port = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 4096
    reason = refusal(request) or (None if port in PORTS else f"the port {port} is not allowed")
    if reason is not None:
        sys.stdout.write(json.dumps({"refused": reason}))
        return 3
    sys.stdout.write(json.dumps(forward(request, port)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
