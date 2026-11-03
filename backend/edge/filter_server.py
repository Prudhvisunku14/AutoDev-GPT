"""The API filter's server: the only approved route from DevPilot to OpenCode (Phase 4D Stage 7).

It decides nothing itself. filter_policy.py holds every rule, and this file moves bytes around them.
Standard library only: it runs in a container with nothing installed, as `python3 -I filter_server.py`.

- One request per connection, so a second request hidden in the same stream is never read.
- The HTTP parsing is its own (filter_policy.parse_head), not `http.server`, which accepts folded
  headers, bare line feeds and HTTP/0.9.
- The upstream request is new. The client's headers, query and body text are never copied.
- The client's token proves nothing to OpenCode, and OpenCode's password never reaches the client.
- Every decision is one JSON line in the audit log, with no header value, query or body in it.

Credentials come only from the environment, by name: DEVPILOT_FILTER_TOKEN and
OPENCODE_SERVER_PASSWORD. A run is refused if either is missing or the token is short.
"""

from __future__ import annotations

import argparse
import base64
import http.client
import json
import os
import socket
import socketserver
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, TextIO

if not __package__:  # run as a script from a read-only mount, as <mount>/edge/filter_server.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from edge.filter_policy import (  # noqa: E402
    FILTER_SERVICE,
    MAX_EVENT_BYTES,
    MAX_HEAD_BYTES,
    MAX_RESPONSE_BYTES,
    MIN_TOKEN_CHARS,
    PASSWORD_ENV,
    TOKEN_ENV,
    EventLimiter,
    Operation,
    Policy,
    Refused,
    answer_problem,
    audit_target,
    authenticated,
    check_body,
    check_framing,
    find_route,
    parse_head,
)

CLIENT_SECONDS = 15.0
UPSTREAM_SECONDS = 30.0
STREAM_IDLE_SECONDS = 30.0
STREAM_MAX_SECONDS = 900.0
MAX_CONNECTIONS = 32
_DRAIN_SECONDS = 0.1
_STATUS_TEXT = {200: "OK", 204: "No Content", 400: "Bad Request", 401: "Unauthorized", 402: "Payment Required", 403: "Forbidden", 404: "Not Found", 408: "Request Timeout",
                413: "Content Too Large", 429: "Too Many Requests", 431: "Request Header Fields Too Large", 502: "Bad Gateway", 503: "Service Unavailable", 504: "Gateway Timeout"}  # fmt: skip
_CONFIG_KEYS = frozenset({"directory", "provider_id", "model_id", "session_rules", "upstream_host", "upstream_port"})
BUSY_RECORD = {"method": "?", "target": "?", "status": 503, "forwarded": False, "reason": "every connection slot is in use", "ms": 0}


class UpstreamReply(Protocol):
    status: int
    media: str

    def read(self, amount: int) -> bytes: ...

    def close(self) -> None: ...


class Upstream(Protocol):
    def send(self, method: str, target: str, body: bytes | None, *, stream: bool) -> UpstreamReply: ...


class Connections(Protocol):
    """Serves one request on one connection. The filter and the model gateway both do."""

    def handle(self, conn: socket.socket) -> None: ...

    def busy(self) -> None:
        """Record a connection refused for want of a slot. Nothing was read from it."""
        ...


class _HttpReply:
    def __init__(self, connection: http.client.HTTPConnection, response: http.client.HTTPResponse) -> None:
        self._connection = connection
        self._response = response
        self.status = response.status
        self.media = response.getheader("content-type") or ""

    def read(self, amount: int) -> bytes:
        return self._response.read1(amount)

    def close(self) -> None:
        self._connection.close()


class HttpUpstream:
    """OpenCode, with its password. A new connection and fixed headers for each request."""

    def __init__(self, host: str, port: int, password: str) -> None:
        self._host = host
        self._port = port
        self._authorization = "Basic " + base64.b64encode(f"opencode:{password}".encode()).decode("ascii")

    def send(self, method: str, target: str, body: bytes | None, *, stream: bool) -> UpstreamReply:
        timeout = STREAM_IDLE_SECONDS if stream else UPSTREAM_SECONDS
        connection = http.client.HTTPConnection(self._host, self._port, timeout=timeout)
        headers = {"Authorization": self._authorization, "Accept": "application/json, text/event-stream", "Connection": "close"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            connection.request(method, target, body=body, headers=headers)
            return _HttpReply(connection, connection.getresponse())
        except BaseException:
            connection.close()
            raise


class Filter:
    def __init__(self, policy: Policy, token: str, upstream: Upstream, *, audit: Callable[[dict[str, Any]], None]) -> None:
        self._policy = policy
        self._token = token
        self._upstream = upstream
        self._audit = audit

    def handle(self, conn: socket.socket) -> None:
        """Serve one request on one connection, then the caller closes it."""
        record: dict[str, Any] = {"method": "?", "target": "?", "status": 0, "forwarded": False}
        began = time.monotonic()
        conn.settimeout(CLIENT_SECONDS)
        try:
            self._serve(conn, record)
        except Refused as exc:
            record.update(status=exc.status, reason=exc.reason)
            reply_error(conn, exc)
        except TimeoutError:
            record.update(status=408, reason="the client was too slow")
            reply_error(conn, Refused(408, "the client was too slow"))
        except OSError as exc:
            record.update(status=record["status"] or 499, reason=f"the connection failed ({type(exc).__name__})")
        finally:
            record["ms"] = round((time.monotonic() - began) * 1000)
            self._audit(record)

    def busy(self) -> None:
        self._audit(dict(BUSY_RECORD))

    def _serve(self, conn: socket.socket, record: dict[str, Any]) -> None:
        raw, rest = read_head(conn)
        head = parse_head(raw)
        record.update(method=head.method[:16], target=audit_target(head.target))
        length = check_framing(head)
        if not authenticated(head, self._token):
            raise Refused(401, "the filter token is missing or wrong")
        route = find_route(head, self._policy)
        record["operation"] = route.operation.template
        if route.operation.answer == "local":
            record["status"] = 200
            send_answer(conn, 200, "application/json", json.dumps({"service": FILTER_SERVICE, "healthy": True}).encode())
            return
        body = check_body(route.operation, head, read_body(conn, rest, length), self._policy)
        self._forward(conn, route.operation, route.upstream_target, body, record)

    def _forward(self, conn: socket.socket, operation: Operation, target: str, body: bytes | None, record: dict[str, Any]) -> None:
        stream = operation.answer == "stream"
        record["forwarded"] = True
        try:
            reply = self._upstream.send(operation.method, target, body, stream=stream)
        except TimeoutError as exc:
            raise Refused(504, "the upstream did not answer in time") from exc
        except (OSError, http.client.HTTPException) as exc:
            raise Refused(502, f"the upstream could not be reached ({type(exc).__name__})") from exc
        try:
            problem = answer_problem(operation, reply.status, reply.media)
            if problem is not None:
                raise Refused(502, problem)
            record["status"] = reply.status
            if stream:
                _relay_stream(conn, reply)
                return
            data = read_all(reply)
            send_answer(conn, reply.status, "application/json" if reply.status != 204 else "", data)
            record["bytes"] = len(data)
        finally:
            reply.close()


def reply_error(conn: socket.socket, refusal: Refused) -> None:
    # Nothing about the rules for a caller who has not shown the token.
    public = refusal.reason if refusal.status == 403 else _STATUS_TEXT.get(refusal.status, "refused")
    try:
        send_answer(conn, refusal.status, "application/json", refusal.body if refusal.body is not None else json.dumps({"error": public}).encode())
        _drain(conn)
    except OSError:
        pass


def _drain(conn: socket.socket) -> None:
    """Read what the client is still sending, because closing over unread bytes can destroy the reply."""
    conn.shutdown(socket.SHUT_WR)
    deadline = time.monotonic() + _DRAIN_SECONDS
    while (left := deadline - time.monotonic()) > 0:  # a total limit: a client that keeps sending gets no longer
        conn.settimeout(left)
        if not conn.recv(65536):
            return


def read_head(conn: socket.socket) -> tuple[bytes, bytes]:
    data = bytearray()
    while True:
        end = data.find(b"\r\n\r\n")
        if end >= 0:
            if end > MAX_HEAD_BYTES:
                raise Refused(431, "the request head is too large")
            return bytes(data[: end + 4]), bytes(data[end + 4 :])
        if b"\n" in data.replace(b"\r\n", b""):  # it can never become a valid head, so do not wait for one
            raise Refused(400, "a stray line ending in the request head")
        if len(data) > MAX_HEAD_BYTES:
            raise Refused(431, "the request head is too large")
        chunk = conn.recv(4096)
        if not chunk:
            raise Refused(400, "the connection closed inside the request head")
        data.extend(chunk)


def read_body(conn: socket.socket, rest: bytes, length: int) -> bytes:
    if len(rest) > length:
        raise Refused(400, "more bytes than the declared length")
    data = bytearray(rest)
    while len(data) < length:
        chunk = conn.recv(min(65536, length - len(data)))
        if not chunk:
            raise Refused(400, "the connection closed inside the body")
        data.extend(chunk)
    return bytes(data)


def read_all(reply: UpstreamReply) -> bytes:
    data = bytearray()
    while True:
        chunk = reply.read(65536)
        if not chunk:
            return bytes(data)
        data.extend(chunk)
        if len(data) > MAX_RESPONSE_BYTES:
            raise Refused(502, "the upstream answer is too large")


def _relay_stream(conn: socket.socket, reply: UpstreamReply) -> None:
    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n")
    limiter = EventLimiter(MAX_EVENT_BYTES)
    deadline = time.monotonic() + STREAM_MAX_SECONDS
    while time.monotonic() < deadline:
        try:
            chunk = reply.read(65536)
        except TimeoutError:
            return  # idle: the client reconnects
        if not chunk or not limiter.accept(chunk):
            return
        conn.sendall(chunk)


def send_answer(conn: socket.socket, status: int, media: str, body: bytes, headers: dict[str, str] | None = None) -> None:
    lines = [f"HTTP/1.1 {status} {_STATUS_TEXT.get(status, 'Status')}"]
    if status != 204:
        lines += [f"Content-Type: {media}", f"Content-Length: {len(body)}"]
    for name, value in (headers or {}).items():  # names fixed in DevPilot's code; values are checked to be plain digits
        if not (name.replace("-", "").isalpha() and value.isdigit()):
            raise ValueError(f"refusing to send the header {name!r}")
        lines.append(f"{name}: {value}")
    lines += ["Cache-Control: no-store", "Connection: close", "", ""]
    conn.sendall("\r\n".join(lines).encode("ascii") + (b"" if status == 204 else body))


class JsonLines:
    """One JSON line per decision. Only the filter's own fields are ever written."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self._lock = threading.Lock()

    def __call__(self, record: dict[str, Any]) -> None:
        line = json.dumps({"ts": round(time.time(), 3), **record}, ensure_ascii=True)
        with self._lock:
            self._stream.write(line + "\n")
            self._stream.flush()


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 64
    on_no_thread: Callable[[socket.socket], None]

    def process_request(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request(request, client_address)
        except RuntimeError:  # the container's process limit: nothing was read, so this is a refusal like a full house
            self.on_no_thread(request)
            self.shutdown_request(request)


def build_server(listen: tuple[str, int], filter_: Filter | Connections) -> _Server:
    slots = threading.BoundedSemaphore(MAX_CONNECTIONS)

    def refuse(conn: socket.socket) -> None:
        """Nothing was read and nothing is forwarded. The drain is what keeps the 503 from being lost to a reset."""
        reply_error(conn, Refused(503, "every connection slot is in use"))
        filter_.busy()

    class Handler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            if not slots.acquire(blocking=False):
                refuse(self.request)
                return
            try:
                filter_.handle(self.request)
            finally:
                slots.release()

    server = _Server(listen, Handler)
    server.on_no_thread = refuse
    return server


def load_config(path: Path) -> dict[str, Any]:
    config = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict) or set(config) != _CONFIG_KEYS:
        raise ValueError("the configuration file does not have exactly the expected keys")
    if not isinstance(config["upstream_host"], str) or not isinstance(config["upstream_port"], int):
        raise ValueError("the configuration file has no usable upstream")
    return config


def credentials(environ: dict[str, str] | os._Environ[str]) -> tuple[str, str]:
    token, password = environ.get(TOKEN_ENV, ""), environ.get(PASSWORD_ENV, "")
    if len(token) < MIN_TOKEN_CHARS:
        raise ValueError(f"{TOKEN_ENV} is missing or shorter than {MIN_TOKEN_CHARS} characters")
    if not password:
        raise ValueError(f"{PASSWORD_ENV} is missing")
    return token, password


def main() -> int:
    parser = argparse.ArgumentParser(description="DevPilot's API filter in front of OpenCode")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=8080)
    parser.add_argument("--audit", required=True, type=Path)
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        policy = Policy.from_config(config)
        token, password = credentials(os.environ)
    except (OSError, ValueError) as exc:
        print(f"the filter did not start: {exc}", file=sys.stderr)
        return 2
    with args.audit.open("a", encoding="utf-8", buffering=1) as stream:
        upstream = HttpUpstream(config["upstream_host"], config["upstream_port"], password)
        server = build_server((args.listen_host, args.listen_port), Filter(policy, token, upstream, audit=JsonLines(stream)))
        with server:
            server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
