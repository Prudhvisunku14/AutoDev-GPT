"""The filter's server, end to end over a local socket pair, with an in-memory upstream object.

No HTTP server stands in for OpenCode: `FakeUpstream` is a plain object behind the `Upstream` Protocol.
The real upstream path is exercised by scripts/opencode_filter_check.py, once, with approval.
"""

from __future__ import annotations

import contextlib
import json
import socket
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from app.coding.opencode_policy import session_rules
from edge import filter_server
from edge.filter_policy import FILTER_SERVICE, MAX_HEAD_BYTES, MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, Policy
from edge.filter_server import Filter, HttpUpstream, credentials, load_config

TOKEN = "T" * 43
PASSWORD = "the-opencode-password-never-leaves"
QUERY = "directory=%2Fworkspace"
SESSION = "ses_abc123"
POLICY = Policy.from_config({"directory": "/workspace", "provider_id": "openai", "model_id": "m", "session_rules": session_rules()})


class FakeReply:
    def __init__(self, status: int, media: str, body: bytes = b"", *, fail_after: int | None = None) -> None:
        self.status = status
        self.media = media
        self._body = body
        self._fail_after = fail_after
        self.closed = False

    def read(self, amount: int) -> bytes:
        if self._fail_after == 0:
            raise TimeoutError
        if self._fail_after is not None:
            self._fail_after -= 1
        chunk, self._body = self._body[:amount], self._body[amount:]
        return chunk

    def close(self) -> None:
        self.closed = True


class FakeUpstream:
    def __init__(self, reply: FakeReply | Exception | None = None) -> None:
        self.reply = reply or FakeReply(200, "application/json", b'{"ok":true}')
        self.calls: list[tuple[str, str, bytes | None, bool]] = []

    def send(self, method: str, target: str, body: bytes | None, *, stream: bool) -> Any:
        self.calls.append((method, target, body, stream))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class Rig:
    def __init__(self, upstream: FakeUpstream | None = None) -> None:
        self.upstream = upstream or FakeUpstream()
        self.audit: list[dict[str, Any]] = []
        self.filter = Filter(POLICY, TOKEN, self.upstream, audit=self.audit.append)

    def send(self, raw: bytes) -> tuple[int, dict[str, str], bytes]:
        client, server = socket.socketpair()
        client.settimeout(5)
        worker = threading.Thread(target=self._serve, args=(server,))
        worker.start()
        client.sendall(raw)
        client.shutdown(socket.SHUT_WR)
        received = bytearray()
        while chunk := client.recv(65536):
            received.extend(chunk)
        client.close()
        worker.join(5)
        head, _, body = bytes(received).partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        headers = {k.lower(): v.strip() for k, _, v in (line.partition(":") for line in lines[1:])}
        return int(lines[0].split()[1]), headers, body

    def _serve(self, conn: socket.socket) -> None:
        try:
            self.filter.handle(conn)
        finally:
            conn.close()


def _request(method: str, target: str, *headers: str, body: bytes = b"", token: str | None = TOKEN) -> bytes:
    lines = [f"{method} {target} HTTP/1.1", "Host: filter"]
    if token is not None:
        lines.append(f"Authorization: Bearer {token}")
    lines += list(headers)
    if body:
        lines += ["Content-Type: application/json", f"Content-Length: {len(body)}"]
    return ("\r\n".join([*lines, "", ""])).encode("latin-1") + body


def _prompt_body(**changes: Any) -> bytes:
    value: dict[str, Any] = {"agent": "build", "model": {"providerID": "openai", "modelID": "m"}, "parts": [{"type": "text", "text": "do it"}], **changes}
    return json.dumps(value).encode()


# --- Allowed requests -------------------------------------------------------------------------------


def test_the_filter_answers_for_itself_without_touching_the_upstream() -> None:
    rig = Rig()
    status, headers, body = rig.send(_request("GET", "/devpilot/health"))
    assert status == 200 and json.loads(body) == {"service": FILTER_SERVICE, "healthy": True}
    assert headers["content-type"] == "application/json" and rig.upstream.calls == []


def test_an_allowed_read_is_forwarded_in_its_one_form() -> None:
    rig = Rig()
    status, _, body = rig.send(_request("GET", "/config?directory=/workspace"))
    assert status == 200 and body == b'{"ok":true}'
    assert rig.upstream.calls == [("GET", f"/config?{QUERY}", None, False)]


def test_only_the_rebuilt_body_goes_upstream() -> None:
    rig = Rig(FakeUpstream(FakeReply(204, "")))
    status, _, body = rig.send(_request("POST", f"/session/{SESSION}/prompt_async?{QUERY}", body=json.dumps(json.loads(_prompt_body()), indent=2).encode()))
    assert status == 204 and body == b""
    assert rig.upstream.calls[0][2] == json.dumps(json.loads(_prompt_body()), separators=(",", ":")).encode()


def test_the_body_may_arrive_in_pieces() -> None:
    rig = Rig(FakeUpstream(FakeReply(204, "")))
    raw = _request("POST", f"/session/{SESSION}/prompt_async?{QUERY}", body=_prompt_body())
    client, server = socket.socketpair()
    worker = threading.Thread(target=rig._serve, args=(server,))
    worker.start()
    for start in range(0, len(raw), 40):
        client.sendall(raw[start : start + 40])
    client.settimeout(5)
    assert client.recv(4096).startswith(b"HTTP/1.1 204")
    client.close()
    worker.join(5)


def test_the_answer_is_built_fresh_with_nothing_of_the_upstreams_headers() -> None:
    rig = Rig()
    _, headers, _ = rig.send(_request("GET", "/doc"))
    assert set(headers) == {"content-type", "content-length", "cache-control", "connection"}
    assert headers["connection"] == "close"


def test_an_event_stream_is_relayed() -> None:
    rig = Rig(FakeUpstream(FakeReply(200, "text/event-stream", b'data: {"type":"server.connected"}\n\n')))
    status, headers, body = rig.send(_request("GET", f"/event?{QUERY}"))
    assert status == 200 and headers["content-type"] == "text/event-stream" and b"server.connected" in body
    assert rig.upstream.calls[0][3] is True


def test_an_idle_event_stream_ends_quietly() -> None:
    reply = FakeReply(200, "text/event-stream", b"data: {}\n\n", fail_after=1)
    rig = Rig(FakeUpstream(reply))
    status, _, _ = rig.send(_request("GET", f"/event?{QUERY}"))
    assert status == 200 and reply.closed


def test_an_oversized_event_ends_the_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(filter_server, "MAX_EVENT_BYTES", 10)
    rig = Rig(FakeUpstream(FakeReply(200, "text/event-stream", b"data: " + b"x" * 100)))
    _, _, body = rig.send(_request("GET", f"/event?{QUERY}"))
    assert body == b""


# --- Refusals never reach the upstream --------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        _request("GET", "/config", token=None),
        _request("GET", "/config", token="wrong"),
        _request("GET", "/doc", "Authorization: Bearer second"),
        _request("GET", "/"),
        _request("GET", "/index.html"),
        _request("OPTIONS", f"/config?{QUERY}"),
        _request("DELETE", f"/session/{SESSION}?{QUERY}"),
        _request("POST", f"/session/{SESSION}/shell?{QUERY}", body=b'{"command":"id"}'),
        _request("PATCH", f"/config?{QUERY}", body=b"{}"),
        _request("GET", "/%2e%2e/config"),
        _request("GET", f"/session/../config?{QUERY}"),
        _request("GET", "/config?directory=/etc"),
        _request("POST", f"/session/{SESSION}/prompt_async?{QUERY}", body=_prompt_body(tools={"bash": False})),
        _request("POST", f"/session/{SESSION}/prompt_async?{QUERY}", body=_prompt_body(agent="plan")),
        _request("GET", "/doc", "Transfer-Encoding: chunked"),
        _request("GET", "/doc", "Content-Length: 1", "Content-Length: 2"),
        _request("POST", f"/session?{QUERY}", "Content-Type: application/json", "Content-Length: 5", body=b""),
        b"GET /doc HTTP/1.0\r\nHost: x\r\n\r\n",
        b"GET /doc\r\n\r\n",
    ],
    ids=lambda raw: raw.split(b"\r\n", 1)[0].decode("latin-1")[:48],
)
def test_nothing_refused_reaches_the_upstream(raw: bytes) -> None:
    rig = Rig()
    status, _, _ = rig.send(raw)
    assert status in (400, 401, 403) and rig.upstream.calls == []
    assert rig.audit[0]["forwarded"] is False


def test_a_missing_token_learns_nothing_about_the_rules() -> None:
    _, _, known = Rig().send(_request("GET", "/config", token=None))
    _, _, unknown = Rig().send(_request("GET", "/no/such/path", token=None))
    assert known == unknown == b'{"error": "Unauthorized"}'


def test_framing_errors_explain_nothing_either() -> None:
    _, _, body = Rig().send(_request("GET", "/doc", "Transfer-Encoding: chunked"))
    assert body == b'{"error": "Bad Request"}'


def test_a_bare_line_feed_is_refused_without_waiting() -> None:
    assert Rig().send(b"GET /doc HTTP/1.1\nHost: x\n\n")[0] == 400


def test_a_second_request_hidden_after_the_first_is_refused() -> None:
    rig = Rig()
    smuggled = _request("GET", "/doc") + _request("GET", "/pty")
    assert rig.send(smuggled)[0] == 400 and rig.upstream.calls == []


def test_extra_bytes_after_a_declared_body_are_refused() -> None:
    rig = Rig()
    assert rig.send(_request("POST", f"/session/{SESSION}/prompt_async?{QUERY}", body=_prompt_body()) + b"GET /pty HTTP/1.1\r\n\r\n")[0] == 400
    assert rig.upstream.calls == []


def test_a_body_shorter_than_declared_is_refused() -> None:
    rig = Rig()
    raw = _request("POST", f"/session?{QUERY}", "Content-Type: application/json", "Content-Length: 500") + b"{}"
    assert rig.send(raw)[0] == 400 and rig.upstream.calls == []


def test_a_head_that_never_ends_is_refused() -> None:
    rig = Rig()
    assert rig.send(b"GET /doc HTTP/1.1\r\nX: " + b"a" * (MAX_HEAD_BYTES + 100))[0] == 431


def test_a_huge_finished_head_is_refused() -> None:
    assert Rig().send(_request("GET", "/doc", "X: " + "a" * (MAX_HEAD_BYTES + 100)))[0] == 431


def test_a_head_cut_short_is_refused() -> None:
    assert Rig().send(b"GET /doc HTTP/1.1\r\nHost")[0] == 400


def test_a_body_over_the_limit_is_refused_before_it_is_read() -> None:
    rig = Rig()
    raw = _request("POST", f"/session?{QUERY}", "Content-Type: application/json", f"Content-Length: {MAX_REQUEST_BYTES + 1}")
    assert rig.send(raw)[0] == 413 and rig.upstream.calls == []


def test_a_slow_client_is_cut_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(filter_server, "CLIENT_SECONDS", 0.05)
    rig = Rig()
    client, server = socket.socketpair()
    worker = threading.Thread(target=rig._serve, args=(server,))
    worker.start()
    client.sendall(b"GET /doc HTTP/1.1\r\n")
    worker.join(5)
    client.settimeout(5)
    assert client.recv(4096).startswith(b"HTTP/1.1 408")
    client.close()
    assert rig.audit[0]["status"] == 408


# --- What comes back --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (FakeReply(200, "text/html; charset=utf-8", b"<html>the web interface</html>"), 502),
        (FakeReply(200, "", b"{}"), 502),
        (FakeReply(302, "application/json", b"{}"), 502),
        (FakeReply(401, "application/json", b'{"secret":"detail"}'), 502),
        (FakeReply(500, "application/json", b"{}"), 502),
    ],
)
def test_an_unexpected_answer_stops_at_the_filter(reply: FakeReply, expected: int) -> None:
    status, _, body = Rig(FakeUpstream(reply)).send(_request("GET", f"/config?{QUERY}"))
    assert status == expected and b"html" not in body and b"secret" not in body
    assert reply.closed


def test_a_404_from_the_upstream_is_passed_on() -> None:
    status, _, body = Rig(FakeUpstream(FakeReply(404, "application/json", b'{"name":"NotFoundError"}'))).send(_request("GET", f"/session/{SESSION}?{QUERY}"))
    assert status == 404 and b"NotFoundError" in body


def test_an_oversized_answer_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(filter_server, "MAX_RESPONSE_BYTES", 10)
    reply = FakeReply(200, "application/json", b"x" * 100)
    assert Rig(FakeUpstream(reply)).send(_request("GET", "/doc"))[0] == 502 and reply.closed
    assert MAX_RESPONSE_BYTES > 10


def test_a_timeout_or_a_failure_upstream_is_a_gateway_error() -> None:
    assert Rig(FakeUpstream(TimeoutError())).send(_request("GET", "/doc"))[0] == 504
    assert Rig(FakeUpstream(ConnectionRefusedError())).send(_request("GET", "/doc"))[0] == 502


def test_a_prompt_whose_answer_timed_out_is_a_gateway_error_the_adapter_treats_as_ambiguous() -> None:
    from app.coding.session_control import SubmitVerdict, classify_submission

    rig = Rig(FakeUpstream(TimeoutError()))
    status, _, _ = rig.send(_request("POST", f"/session/{SESSION}/prompt_async?{QUERY}", body=_prompt_body()))
    assert status == 504 and classify_submission(status) is SubmitVerdict.AMBIGUOUS


def test_a_client_that_vanishes_does_not_break_the_filter() -> None:
    rig = Rig()
    client, server = socket.socketpair()
    client.close()
    rig._serve(server)
    assert rig.audit[0]["status"] in (400, 499)


# --- Credentials and the audit log ------------------------------------------------------------------


def test_no_credential_reaches_the_audit_log() -> None:
    rig = Rig()
    rig.send(_request("GET", "/doc"))
    rig.send(_request("GET", "/doc", token="a-wrong-token-that-looks-real"))
    rig.send(_request("GET", "/config?directory=/etc"))
    rig.send(_request("POST", f"/session/{SESSION}/prompt_async?{QUERY}", body=_prompt_body(system="the-brief-text")))
    text = json.dumps(rig.audit)
    assert len(rig.audit) == 4 and TOKEN not in text and "a-wrong-token" not in text
    assert "/etc" not in text and "the-brief-text" not in text


def test_the_audit_log_records_each_decision() -> None:
    rig = Rig()
    rig.send(_request("GET", "/pty"))
    entry = rig.audit[0]
    assert (entry["method"], entry["target"], entry["status"], entry["forwarded"]) == ("GET", "/pty", 403, False)
    assert entry["reason"] == "this operation is not allowed"


def test_the_upstream_password_is_never_given_to_a_client() -> None:
    upstream = HttpUpstream("127.0.0.1", 4096, PASSWORD)
    assert PASSWORD not in repr(vars(upstream)) and upstream._authorization.startswith("Basic ")


@pytest.mark.parametrize(
    ("environ", "reason"),
    [({}, "DEVPILOT_FILTER_TOKEN"), ({"DEVPILOT_FILTER_TOKEN": "short", "OPENCODE_SERVER_PASSWORD": "p"}, "DEVPILOT_FILTER_TOKEN"), ({"DEVPILOT_FILTER_TOKEN": TOKEN}, "OPENCODE_SERVER_PASSWORD")],
)
def test_the_filter_will_not_start_without_real_credentials(environ: dict[str, str], reason: str) -> None:
    with pytest.raises(ValueError, match=reason) as caught:
        credentials(environ)
    assert TOKEN not in str(caught.value)


def test_credentials_come_from_the_environment_by_name() -> None:
    assert credentials({"DEVPILOT_FILTER_TOKEN": TOKEN, "OPENCODE_SERVER_PASSWORD": PASSWORD}) == (TOKEN, PASSWORD)
    source = Path(filter_server.__file__).read_text(encoding="utf-8")
    assert "add_argument(\"--token" not in source and "add_argument(\"--password" not in source


def _config(tmp_path: Path, **changes: Any) -> Path:
    config = {"directory": "/workspace", "provider_id": "openai", "model_id": "m", "session_rules": session_rules(),
              "upstream_host": "127.0.0.1", "upstream_port": 4096, **changes}  # fmt: skip
    path = tmp_path / "filter.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def test_the_configuration_file_is_read_exactly(tmp_path: Path) -> None:
    assert load_config(_config(tmp_path))["upstream_port"] == 4096
    with pytest.raises(ValueError, match="exactly the expected keys"):
        load_config(_config(tmp_path, extra=1))
    with pytest.raises(ValueError, match="upstream"):
        load_config(_config(tmp_path, upstream_port="4096"))


def test_the_server_module_imports_only_the_standard_library_and_its_policy() -> None:
    import ast

    for name in ("filter_server.py", "filter_policy.py"):
        tree = ast.parse((Path(filter_server.__file__).parent / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            for module in names:
                assert module.split(".")[0] not in ("app", "httpx", "pydantic", "sqlalchemy", "requests"), (name, module)


# --- Every slot taken -------------------------------------------------------------------------------


class StalledAudit:
    """An audit log on a disk that has stopped answering, until `release`."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self._open = threading.Event()

    def __call__(self, record: dict[str, Any]) -> None:
        self._open.wait(10)
        self.rows.append(record)

    def release(self) -> None:
        self._open.set()


def _exchange(port: int, raw: bytes) -> int:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
        conn.sendall(raw)
        conn.shutdown(socket.SHUT_WR)
        data = bytearray()
        while chunk := conn.recv(65536):
            data.extend(chunk)
    return int(bytes(data).split(b" ", 2)[1])


def _one_at_a_time(audit: Any, count: int) -> tuple[list[int], FakeUpstream, Any]:
    upstream = FakeUpstream()
    server = filter_server.build_server(("127.0.0.1", 0), Filter(POLICY, TOKEN, upstream, audit=audit))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return [_exchange(server.server_address[1], _request("GET", "/index.html")) for _ in range(count)], upstream, server


def test_refused_requests_sent_one_at_a_time_outrun_a_stalled_audit_log_and_forward_nothing() -> None:
    """A client that waits for each answer can still fill the slots: the answer is sent, and the slot is held
    until the decision is on disk. So a stall in the audit log, not concurrency, is enough to reach 503."""
    audit = StalledAudit()
    answers, upstream, server = _one_at_a_time(audit, filter_server.MAX_CONNECTIONS + 5)
    audit.release()
    deadline = time.monotonic() + 5
    while len(audit.rows) < len(answers) and time.monotonic() < deadline:
        time.sleep(0.01)
    server.shutdown()
    server.server_close()
    assert answers == [403] * filter_server.MAX_CONNECTIONS + [503] * 5
    assert upstream.calls == []
    assert len(audit.rows) == len(answers), "every decision is one line in the audit log, the busy ones too"
    assert [r["status"] for r in audit.rows].count(503) == 5 and not any(r["forwarded"] for r in audit.rows)


def test_with_an_audit_log_that_keeps_up_the_same_requests_are_all_answered() -> None:
    rows: list[dict[str, Any]] = []
    answers, upstream, server = _one_at_a_time(rows.append, filter_server.MAX_CONNECTIONS + 5)
    server.shutdown()
    server.server_close()
    assert answers == [403] * (filter_server.MAX_CONNECTIONS + 5) and upstream.calls == []


def test_a_busy_refusal_reads_nothing_from_the_connection() -> None:
    rig = Rig()
    rig.filter.busy()
    assert rig.audit == [filter_server.BUSY_RECORD] and rig.upstream.calls == []
    assert rig.audit[0] is not filter_server.BUSY_RECORD, "a shared record could be changed by whoever writes it"


def test_with_no_thread_to_be_had_a_connection_is_refused_and_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    """The container's process limit can stop a thread starting. That is a refusal like any other, not a silent close."""
    rows: list[dict[str, Any]] = []
    upstream = FakeUpstream()
    server = filter_server.build_server(("127.0.0.1", 0), Filter(POLICY, TOKEN, upstream, audit=rows.append))

    def no_thread(*_: Any) -> None:
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(filter_server.socketserver.ThreadingMixIn, "process_request", no_thread)
    worker = threading.Thread(target=server.handle_request)
    worker.start()
    status = _exchange(server.server_address[1], _request("GET", "/index.html"))
    worker.join(5)
    server.server_close()
    assert status == 503 and rows == [filter_server.BUSY_RECORD] and upstream.calls == []


def test_a_client_that_keeps_sending_cannot_hold_a_refused_connection_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """A busy refusal drains without holding a slot, so the wait has a total limit, not one per read. The drain
    runs on its own thread, so a drain with no limit fails this test instead of hanging the whole suite."""
    monkeypatch.setattr(filter_server, "_DRAIN_SECONDS", 0.05)
    client, server = socket.socketpair()
    stop = threading.Event()

    def keep_sending() -> None:
        while not stop.is_set():
            try:
                client.sendall(b"x")
            except OSError:
                return
            time.sleep(0.005)

    def drain() -> None:
        with contextlib.suppress(OSError):  # reply_error treats a timeout as the end of the wait, too
            filter_server._drain(server)

    sender = threading.Thread(target=keep_sending, daemon=True)
    drainer = threading.Thread(target=drain, daemon=True)
    sender.start()
    drainer.start()
    drainer.join(1.0)
    stuck = drainer.is_alive()
    stop.set()
    sender.join(5)
    client.close()
    server.close()
    assert not stuck
