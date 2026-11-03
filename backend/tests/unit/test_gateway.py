"""The model gateway, over a local socket pair, with an in-memory upstream object.

Every "OpenAI" answer here is a fake written in this file. No test can reach OpenAI: `FakeUpstream` is a
plain object behind the `Upstream` Protocol, and the real `TlsUpstream` is only built, never sent through.
"""

from __future__ import annotations

import json
import socket
import ssl
import threading
from pathlib import Path
from typing import Any

import pytest

from edge import gateway_server
from edge.filter_policy import Refused, parse_head
from edge.gateway_policy import (
    ALLOWED_FIELDS,
    MAX_BODY_BYTES,
    SHORT_CONTEXT_TOKENS,
    Budget,
    CheckedRequest,
    GatewayPolicy,
    Usage,
    UsageScanner,
    answer_status,
    check_request,
    usage_from,
)
from edge.gateway_server import Gateway, TlsUpstream, credentials, load_policy

TOKEN = "G" * 43
KEY = "the-provider-key-never-leaves-the-gateway"
PATH = "/v1/responses"
POLICY = GatewayPolicy(model="gpt-test", max_output_tokens=1_000, max_requests=3, max_tokens=50_000)
DONE = b'event: response.completed\ndata: {"type":"response.completed","response":{"usage":{"input_tokens":120,"output_tokens":30,"input_tokens_details":{"cached_tokens":20}}}}\n\n'
STREAM = b'event: response.output_text.delta\ndata: {"delta":"hi"}\n\n' + DONE


class FakeReply:
    def __init__(self, status: int, media: str, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self.status = status
        self.media = media
        self.headers = headers or {}
        self._body = body
        self.closed = False

    def read(self, amount: int) -> bytes:
        chunk, self._body = self._body[:amount], self._body[amount:]
        return chunk

    def close(self) -> None:
        self.closed = True


class FakeUpstream:
    def __init__(self, reply: FakeReply | Exception | None = None) -> None:
        self.reply = reply or FakeReply(200, "text/event-stream", STREAM)
        self.calls: list[tuple[bytes, bool]] = []

    def send(self, body: bytes, *, stream: bool) -> Any:
        self.calls.append((body, stream))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class Rig:
    def __init__(self, upstream: FakeUpstream | None = None, policy: GatewayPolicy = POLICY) -> None:
        self.upstream = upstream or FakeUpstream()
        self.audit: list[dict[str, Any]] = []
        self.journal: list[dict[str, Any]] = []  # every line, in order
        self.ledger: list[dict[str, Any]] = []  # the lines that settle a request
        self.gateway = Gateway(policy, TOKEN, self.upstream, audit=self.audit.append, ledger=self._write)

    def _write(self, row: dict[str, Any]) -> None:
        self.journal.append(row)
        if row["status"] != 0:
            self.ledger.append(row)

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
            self.gateway.handle(conn)
        finally:
            conn.close()


def _body(**changes: Any) -> bytes:
    value: dict[str, Any] = {"model": "gpt-test", "input": [{"role": "user", "content": [{"type": "input_text", "text": "hello"}]}], "stream": True, **changes}
    return json.dumps(value).encode()


def _request(body: bytes = b"", *, target: str = PATH, method: str = "POST", token: str | None = TOKEN) -> bytes:
    lines = [f"{method} {target} HTTP/1.1", "Host: gateway"]
    if token is not None:
        lines.append(f"Authorization: Bearer {token}")
    if body:
        lines += ["Content-Type: application/json", f"Content-Length: {len(body)}"]
    return ("\r\n".join([*lines, "", ""])).encode("latin-1") + body


def _check(body: bytes, policy: GatewayPolicy = POLICY) -> CheckedRequest:
    return check_request(parse_head(b"POST /v1/responses HTTP/1.1\r\nHost: g\r\nContent-Type: application/json\r\n\r\n"), body, policy)


# --- The approved path works ------------------------------------------------------------------------


def test_a_streamed_answer_reaches_the_client_and_is_counted() -> None:
    rig = Rig()
    status, headers, body = rig.send(_request(_body()))
    assert status == 200 and headers["content-type"] == "text/event-stream" and body == STREAM
    assert rig.gateway.budget.tokens == 150 and rig.gateway.budget.requests == 1
    assert rig.ledger[0]["input_tokens"] == 120 and rig.ledger[0]["output_tokens"] == 30 and rig.ledger[0]["usage_known"] is True


def test_a_json_answer_is_counted_too() -> None:
    answer = json.dumps({"usage": {"input_tokens": 7, "output_tokens": 3}}).encode()
    rig = Rig(FakeUpstream(FakeReply(200, "application/json; charset=utf-8", answer)))
    status, _, body = rig.send(_request(_body(stream=False)))
    assert status == 200 and body == answer and rig.gateway.budget.tokens == 10


def test_only_the_rebuilt_body_goes_upstream() -> None:
    rig = Rig()
    rig.send(_request(json.dumps(json.loads(_body()), indent=2).encode()))
    sent, stream = rig.upstream.calls[0]
    assert stream is True and b"\n" not in sent and json.loads(sent)["max_output_tokens"] == 1_000


def test_a_request_keeps_a_smaller_output_cap_of_its_own() -> None:
    assert json.loads(_check(_body(max_output_tokens=10)).body)["max_output_tokens"] == 10


def test_a_larger_output_cap_is_cut_to_the_policy() -> None:
    assert json.loads(_check(_body(max_output_tokens=9_999_999)).body)["max_output_tokens"] == 1_000


def test_the_fields_opencode_really_sends_pass() -> None:
    # Field for field from `OpenAIResponses.fromRequest` in the pinned 1.19.5 binary.
    _check(_body(instructions="be brief", tools=[{"type": "function", "name": "read", "description": "d", "parameters": {"type": "object"}, "strict": False}],
                 tool_choice="auto", store=False, service_tier="auto", prompt_cache_key="k", include=["reasoning.encrypted_content"],
                 reasoning={"effort": "medium", "summary": "auto"}, text={"verbosity": "low"}, max_output_tokens=10, temperature=0.1, top_p=1))  # fmt: skip


def test_the_item_kinds_of_a_long_conversation_pass() -> None:
    _check(_body(input=[
        {"role": "system", "content": "rules"},
        {"role": "user", "content": [{"type": "input_text", "text": "a"}, {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}]},
        {"role": "assistant", "content": [{"type": "output_text", "text": "b"}]},
        {"type": "reasoning", "id": "r1", "summary": [{"type": "summary_text", "text": "s"}], "encrypted_content": "e"},
        {"type": "item_reference", "id": "i1"},
        {"type": "function_call", "call_id": "c1", "name": "read", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "text"},
    ]))  # fmt: skip


# --- Authentication ---------------------------------------------------------------------------------


@pytest.mark.parametrize("token", [None, "", "wrong", TOKEN + "x", TOKEN[:-1]])
def test_every_request_needs_this_runs_token(token: str | None) -> None:
    rig = Rig()
    status, _, body = rig.send(_request(_body(), token=token))
    assert status == 401 and rig.upstream.calls == [] and b"token" not in body.lower()


def test_a_missing_token_learns_nothing_about_the_rules() -> None:
    rig = Rig()
    assert rig.send(_request(_body(model="other"), token=None))[0] == 401
    assert rig.gateway.budget.requests == 0


# --- Operations -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "target"),
    [("GET", PATH), ("POST", "/v1/chat/completions"), ("POST", "/v1/embeddings"), ("POST", "/v1/files"), ("POST", "/v1/responses?x=1"),
     ("POST", "/v1/responses/"), ("POST", "/v1/responses/r1/cancel"), ("DELETE", PATH), ("POST", "/v1/models"), ("GET", "/")],
)  # fmt: skip
def test_one_operation_exists(method: str, target: str) -> None:
    rig = Rig()
    assert rig.send(_request(_body(), target=target, method=method))[0] == 403
    assert rig.upstream.calls == []


# --- What a request may hold ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"model": "gpt-expensive"}, "approved model"),
        ({"background": True}, "not allowed"),
        ({"previous_response_id": "r1"}, "not allowed"),
        ({"conversation": "c1"}, "not allowed"),
        ({"prompt": {"id": "p"}}, "not allowed"),
        ({"surprise": 1}, "not allowed"),
        ({"tools": [{"type": "web_search"}]}, "hosted tools"),
        ({"tools": [{"type": "file_search"}]}, "hosted tools"),
        ({"tools": [{"type": "code_interpreter"}]}, "hosted tools"),
        ({"tools": [{"type": "computer_use_preview"}]}, "hosted tools"),
        ({"tools": [{"type": "mcp", "server_url": "https://x"}]}, "hosted tools"),
        ({"tools": [{"type": "function"}, {"type": "image_generation"}]}, "hosted tools"),
        ({"tools": "web_search"}, "allowed size"),
        ({"tools": [{"type": "function"}] * 65}, "allowed size"),
        ({"tool_choice": {"type": "web_search"}}, "hosted tool"),
        ({"tool_choice": "anything"}, "hosted tool"),
        ({"tool_choice": {"type": "function", "name": "x", "extra": 1}}, "hosted tool"),
        ({"include": ["web_search_call.results"]}, "include"),
        ({"include": "reasoning.encrypted_content"}, "include"),
        ({"service_tier": "priority"}, "service tier"),
        ({"max_output_tokens": 0}, "positive"),
        ({"max_output_tokens": True}, "positive"),
        ({"stream": "yes"}, "true or false"),
        ({"input": []}, "no input"),
        ({"input": None}, "no input"),
        ({"input": [1]}, "not an object"),
        ({"input": [{"type": "web_search_call"}]}, "not allowed"),
        ({"input": [{"type": "local_shell_call"}]}, "not allowed"),
        ({"input": [{"role": "user", "content": [{"type": "input_file", "file_id": "f"}]}]}, "not an allowed type"),
        ({"input": [{"role": "user", "content": ["text"]}]}, "not an allowed type"),
        ({"input": [{"role": "user", "content": [{"type": "input_image", "image_url": "https://evil.example/x.png"}]}]}, "fetch"),
    ],
)  # fmt: skip
def test_a_request_outside_the_rules_is_refused(changes: dict[str, Any], reason: str) -> None:
    with pytest.raises(Refused, match=reason) as caught:
        _check(_body(**changes))
    assert caught.value.status == 403


@pytest.mark.parametrize("body", [b"[]", b'"text"'])
def test_a_body_that_is_not_an_object_is_refused(body: bytes) -> None:
    with pytest.raises(Refused) as caught:
        _check(body)
    assert caught.value.status == 403


@pytest.mark.parametrize("body", [b"{", b"not json", b'{"a":NaN}'])
def test_json_two_parsers_could_read_differently_is_refused(body: bytes) -> None:
    with pytest.raises(Refused):
        _check(body)


def test_duplicate_keys_are_refused() -> None:
    with pytest.raises(Refused, match="duplicate"):
        _check(b'{"model":"gpt-test","model":"gpt-other","input":"x"}')


def test_excess_nesting_is_refused() -> None:
    deep: Any = "x"
    for _ in range(100):
        deep = [deep]
    with pytest.raises(Refused, match="nested"):
        _check(_body(metadata=deep))


def test_the_declared_type_must_be_json() -> None:
    head = parse_head(b"POST /v1/responses HTTP/1.1\r\nHost: g\r\nContent-Type: text/plain\r\n\r\n")
    with pytest.raises(Refused, match="application/json"):
        check_request(head, _body(), POLICY)


def test_a_body_over_the_limit_is_refused_before_it_is_read() -> None:
    rig = Rig()
    head = f"POST {PATH} HTTP/1.1\r\nHost: g\r\nAuthorization: Bearer {TOKEN}\r\nContent-Type: application/json\r\nContent-Length: {MAX_BODY_BYTES + 1}\r\n\r\n"
    assert rig.send(head.encode())[0] == 413


def test_ambiguous_framing_is_refused() -> None:
    rig = Rig()
    raw = f"POST {PATH} HTTP/1.1\r\nHost: g\r\nAuthorization: Bearer {TOKEN}\r\nTransfer-Encoding: chunked\r\nContent-Length: 5\r\n\r\n".encode()
    assert rig.send(raw)[0] == 400 and rig.upstream.calls == []


# --- Budgets ----------------------------------------------------------------------------------------


def test_the_request_budget_stops_the_next_request_before_it_is_forwarded() -> None:
    rig = Rig()
    for _ in range(3):
        assert rig.send(_request(_body()))[0] == 200
    assert rig.send(_request(_body()))[0] == 402  # not 429: that is what a client retries
    assert len(rig.upstream.calls) == 3


def test_the_token_budget_stops_a_request_that_could_not_fit() -> None:
    ceiling = len(_body()) + 512
    rig = Rig(policy=GatewayPolicy(model="gpt-test", max_output_tokens=1_000, max_requests=99, max_tokens=ceiling + 149))
    assert rig.send(_request(_body()))[0] == 200  # 150 used
    assert rig.send(_request(_body()))[0] == 402
    assert len(rig.upstream.calls) == 1


def test_the_output_cap_shrinks_to_what_the_budget_has_left() -> None:
    body = _body()
    room = 500
    policy = GatewayPolicy(model="gpt-test", max_output_tokens=1_000, max_requests=9, max_tokens=len(body) + 512 + room)
    rig = Rig(policy=policy)
    rig.send(_request(body))
    assert json.loads(rig.upstream.calls[0][0])["max_output_tokens"] == room


def test_the_input_ceiling_is_a_true_upper_bound() -> None:
    body = _body(input="word " * 1_000)
    checked = _check(body)
    # Even one token per byte of the whole request could not pass it, and real text takes far fewer.
    assert checked.input_ceiling >= len(body)


@pytest.mark.parametrize(("spent", "allowed"), [(0.0, True), (0.9, False)])
def test_the_spending_limit_counts_what_is_already_spent(spent: float, allowed: bool) -> None:
    policy = GatewayPolicy(model="gpt-test", max_output_tokens=1_000, max_requests=9, max_tokens=10_000_000, max_usd=1.0, price_input_per_mtok=2.0, price_output_per_mtok=100.0)
    budget = Budget(policy)
    budget.usd = spent
    request = CheckedRequest(b"{}", True, 5_000, 1_000)  # at most 0.01 + 0.1
    if allowed:
        assert budget.reserve(request).usd == pytest.approx(0.11)
    else:
        with pytest.raises(Refused) as caught:
            budget.reserve(request)
        assert caught.value.status == 402


def test_outstanding_requests_hold_their_money_until_they_settle() -> None:
    policy = GatewayPolicy(model="gpt-test", max_output_tokens=1_000, max_requests=9, max_tokens=10_000_000, max_usd=0.2, price_input_per_mtok=2.0, price_output_per_mtok=100.0)
    budget = Budget(policy)
    request = CheckedRequest(b"{}", True, 5_000, 1_000)
    first = budget.reserve(request)
    assert budget.outstanding == 1
    with pytest.raises(Refused):
        budget.reserve(request)  # 0.11 held and 0.11 more would pass 0.20
    assert budget.outstanding == 1
    budget.settle(first, Usage(100, 10))
    assert budget.outstanding == 0 and budget.reserve(request).usd > 0


def test_the_budget_never_passes_its_ceiling_however_requests_end() -> None:
    policy = GatewayPolicy(model="gpt-test", max_output_tokens=1_000, max_requests=500, max_tokens=10_000_000, max_usd=1.0, price_input_per_mtok=2.0, price_output_per_mtok=10.0)
    budget = Budget(policy)
    request = CheckedRequest(b"{}", True, 10_000, 1_000)
    settled = 0
    while True:
        try:
            held = budget.reserve(request)
        except Refused:
            break
        budget.settle(held, None if settled % 3 == 0 else Usage(10_000, 1_000))  # every third answer is lost
        settled += 1
    assert settled > 10 and budget.usd <= 1.0 and budget.outstanding == 0


def test_requests_running_together_cannot_both_spend_the_same_room() -> None:
    policy = GatewayPolicy(model="gpt-test", max_output_tokens=100, max_requests=9, max_tokens=150)
    budget = Budget(policy)
    request = CheckedRequest(b"{}", True, 10, 100)
    budget.reserve(request)
    with pytest.raises(Refused) as caught:
        budget.reserve(CheckedRequest(b"{}", True, 40, 100))
    assert caught.value.status == 402


def test_an_answer_with_no_usage_is_charged_its_whole_reservation() -> None:
    rig = Rig(FakeUpstream(FakeReply(200, "text/event-stream", b'data: {"delta":"hi"}\n\n')))
    rig.send(_request(_body()))
    assert rig.ledger[0]["usage_known"] is False and rig.gateway.budget.tokens >= 1_000


def test_a_failed_request_is_charged_too() -> None:
    rig = Rig(FakeUpstream(OSError("reset")))
    assert rig.send(_request(_body()))[0] == 502
    assert rig.gateway.budget.requests == 1 and rig.gateway.budget.tokens >= 1_000


def test_a_spending_limit_refuses_what_could_overrun_it() -> None:
    policy = GatewayPolicy(model="gpt-test", max_output_tokens=1_000, max_requests=9, max_tokens=10_000_000, max_usd=0.001, price_input_per_mtok=5.0, price_output_per_mtok=15.0)
    assert Rig(policy=policy).send(_request(_body()))[0] == 402


def test_the_cost_is_recorded_when_prices_are_set() -> None:
    policy = GatewayPolicy(model="gpt-test", max_output_tokens=1_000, max_requests=9, max_tokens=50_000, max_usd=1.0, price_input_per_mtok=5.0, price_output_per_mtok=15.0)
    rig = Rig(policy=policy)
    rig.send(_request(_body()))
    assert rig.ledger[0]["cost_usd"] == pytest.approx((120 * 5 + 30 * 15) / 1_000_000)
    assert rig.gateway.budget.usd == pytest.approx(rig.ledger[0]["cost_usd"])


# --- Upstream answers -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "media", "stream", "expected"),
    [(200, "text/event-stream", True, 200), (200, "application/json", False, 200), (400, "application/json", True, 400), (429, "application/json", True, 429)],
)
def test_expected_answers_pass(status: int, media: str, stream: bool, expected: int) -> None:
    assert answer_status(status, media, stream=stream) == expected


@pytest.mark.parametrize(
    ("status", "media", "stream"),
    [(401, "application/json", True), (403, "application/json", True), (200, "text/html", True), (200, "text/event-stream", False),
     (301, "text/event-stream", True), (301, "application/json", False), (302, "application/json", False), (302, "text/html", True),
     (500, "application/json", True), (400, "text/html", True)],
)  # fmt: skip
def test_anything_else_never_reaches_the_client(status: int, media: str, stream: bool) -> None:
    with pytest.raises(Refused) as caught:
        answer_status(status, media, stream=stream)
    assert caught.value.status == 502


def test_a_refused_key_is_not_passed_on() -> None:
    rig = Rig(FakeUpstream(FakeReply(401, "application/json", b'{"error":{"message":"bad key sk-abc"}}')))
    status, _, body = rig.send(_request(_body()))
    assert status == 502 and b"sk-abc" not in body and b"key" not in body


def test_an_upstream_timeout_reads_as_a_gateway_timeout() -> None:
    assert Rig(FakeUpstream(TimeoutError())).send(_request(_body()))[0] == 504


def test_rate_limiting_reaches_the_client_so_it_can_back_off() -> None:
    rig = Rig(FakeUpstream(FakeReply(429, "application/json", b'{"error":{"message":"slow down"}}')))
    assert rig.send(_request(_body()))[0] == 429


def test_an_oversized_answer_is_cut_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gateway_server, "MAX_RESPONSE_BYTES", 10)
    rig = Rig(FakeUpstream(FakeReply(200, "application/json", b"x" * 100)))
    assert rig.send(_request(_body(stream=False)))[0] == 502


def test_every_reply_is_closed() -> None:
    reply = FakeReply(200, "text/event-stream", STREAM)
    Rig(FakeUpstream(reply)).send(_request(_body()))
    assert reply.closed


# --- Reading usage ----------------------------------------------------------------------------------


def test_usage_is_found_across_chunk_boundaries() -> None:
    scanner = UsageScanner()
    for start in range(0, len(STREAM), 7):
        scanner.feed(STREAM[start : start + 7])
    assert scanner.usage == Usage(120, 30, 20)


@pytest.mark.parametrize("name", [b"response.incomplete", b"response.failed"])
def test_every_final_event_reports_usage(name: bytes) -> None:
    scanner = UsageScanner()
    scanner.feed(DONE.replace(b"response.completed", name))
    assert scanner.usage == Usage(120, 30, 20)


def test_an_unreadable_final_event_leaves_usage_unknown() -> None:
    scanner = UsageScanner()
    scanner.feed(b"event: response.completed\ndata: {broken\n\n")
    assert scanner.usage is None


def test_an_oversized_event_is_dropped_not_held() -> None:
    scanner = UsageScanner(limit=100)
    scanner.feed(b"x" * 1_000)
    assert len(scanner._buffer) == 0


@pytest.mark.parametrize(
    "payload",
    [None, [], {}, {"usage": None}, {"usage": {"input_tokens": 1}}, {"usage": {"input_tokens": -1, "output_tokens": 1}}, {"usage": {"input_tokens": True, "output_tokens": 1}}],
)
def test_usage_that_is_not_really_there_is_none(payload: Any) -> None:
    assert usage_from(payload) is None


# --- Secrets and logs -------------------------------------------------------------------------------


def test_no_credential_or_prompt_reaches_the_logs() -> None:
    rig = Rig()
    rig.send(_request(_body(instructions="a-secret-brief")))
    rig.send(_request(_body(), token="a-wrong-token-that-looks-real"))
    text = json.dumps([rig.audit, rig.ledger])
    assert TOKEN not in text and "a-wrong-token" not in text and "a-secret-brief" not in text and "hello" not in text


def test_the_ledger_holds_only_numbers_and_the_model() -> None:
    rig = Rig()
    rig.send(_request(_body()))
    assert set(rig.ledger[0]) == {"request", "ts", "model", "status", "input_tokens", "output_tokens", "cached_tokens", "charged_tokens", "cost_usd", "usage_known"}


def test_the_key_goes_upstream_and_never_to_the_client() -> None:
    upstream = TlsUpstream(KEY)
    assert upstream._authorization == f"Bearer {KEY}"
    rig = Rig()
    _, headers, body = rig.send(_request(_body()))
    assert KEY not in json.dumps(headers) and KEY.encode() not in body
    assert set(headers) == {"content-type", "cache-control", "connection"}


def test_certificates_are_verified_and_the_upstream_is_fixed() -> None:
    upstream = TlsUpstream(KEY)
    assert upstream._context.verify_mode is ssl.CERT_REQUIRED and upstream._context.check_hostname is True
    assert gateway_server.UPSTREAM_HOST == "api.openai.com" and gateway_server.UPSTREAM_PORT == 443


def test_the_config_cannot_name_an_upstream(tmp_path: Path) -> None:
    path = tmp_path / "gateway.json"
    path.write_text(json.dumps({"model": "m", "max_output_tokens": 1, "max_requests": 1, "max_tokens": 1, "upstream": "https://evil.example"}), encoding="utf-8")
    assert not hasattr(load_policy(path), "upstream")


# --- Configuration ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "config",
    [{}, {"model": "", "max_output_tokens": 1, "max_requests": 1, "max_tokens": 1}, {"model": "openai/m", "max_output_tokens": 1, "max_requests": 1, "max_tokens": 1},
     {"model": "m", "max_output_tokens": 0, "max_requests": 1, "max_tokens": 1}, {"model": "m", "max_output_tokens": 1, "max_requests": True, "max_tokens": 1},
     {"model": "m", "max_output_tokens": 1, "max_requests": 1, "max_tokens": 1, "price_input_per_mtok": 1.0},
     {"model": "m", "max_output_tokens": 1, "max_requests": 1, "max_tokens": 1, "max_usd": 1.0}],
)  # fmt: skip
def test_a_bad_configuration_never_starts(config: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        GatewayPolicy.from_config(config)


@pytest.mark.parametrize("environ", [{}, {"DEVPILOT_GATEWAY_TOKEN": "short", "DEVPILOT_GATEWAY_OPENAI_KEY": "k"}, {"DEVPILOT_GATEWAY_TOKEN": TOKEN}])
def test_the_gateway_will_not_start_without_real_credentials(environ: dict[str, str]) -> None:
    with pytest.raises(ValueError) as caught:
        credentials(environ)
    assert TOKEN not in str(caught.value)


def test_real_credentials_are_accepted() -> None:
    assert credentials({"DEVPILOT_GATEWAY_TOKEN": TOKEN, "DEVPILOT_GATEWAY_OPENAI_KEY": KEY}) == (TOKEN, KEY)


def test_the_allowlist_holds_nothing_that_detaches_a_request() -> None:
    assert not ALLOWED_FIELDS & {"background", "previous_response_id", "conversation", "prompt"}


def test_a_busy_refusal_is_recorded_and_reaches_nothing() -> None:
    rig = Rig()
    rig.gateway.busy()
    assert rig.audit[-1]["status"] == 503 and rig.audit[-1]["forwarded"] is False
    assert rig.upstream.calls == [] and rig.gateway.budget.requests == 0 and rig.ledger == []


def test_a_request_is_on_the_ledger_before_anything_leaves() -> None:
    """A gateway removed mid-request writes nothing more, so the charge must already be on disk."""
    seen: list[dict[str, Any]] = []

    class Witness(FakeUpstream):
        def send(self, body: bytes, *, stream: bool) -> Any:
            seen.extend(rig.journal)
            return super().send(body, stream=stream)

    rig = Rig(Witness())
    status, _, _ = rig.send(_request(_body()))
    assert status == 200 and len(seen) == 1
    assert seen[0]["status"] == 0 and seen[0]["usage_known"] is False and seen[0]["charged_tokens"] > 0


def test_a_request_is_written_twice_under_one_number_and_settles_for_what_it_used() -> None:
    rig = Rig()
    rig.send(_request(_body()))
    rig.send(_request(_body()))
    assert [(r["request"], r["status"]) for r in rig.journal] == [(1, 0), (1, 200), (2, 0), (2, 200)]
    assert rig.journal[1]["charged_tokens"] < rig.journal[0]["charged_tokens"]


def test_a_refused_request_is_never_on_the_ledger() -> None:
    rig = Rig()
    rig.send(_request(_body(), token="w" * 43))
    assert rig.journal == []


@pytest.mark.parametrize("tier", [None, "auto", "default", "flex"])
def test_every_forwarded_request_is_pinned_to_standard_processing(tier: str | None) -> None:
    """`auto` follows the project's own setting, which could be Fast, at twice the price the budget is worked out on."""
    rig = Rig()
    rig.send(_request(_body(**({"service_tier": tier} if tier else {}))))
    assert json.loads(rig.upstream.calls[0][0])["service_tier"] == "default"


def test_a_request_that_could_reach_long_context_prices_is_refused() -> None:
    """Above this many input tokens the model costs twice as much, so the reservation would be too small. A token
    covers at least a byte, so the size of the body is the proof. The status is not one OpenCode retries."""
    room = SHORT_CONTEXT_TOKENS - 512 - len(_body(input=""))
    assert _check(_body(input="x" * room)).input_ceiling == SHORT_CONTEXT_TOKENS
    with pytest.raises(Refused) as refused:
        _check(_body(input="x" * (room + 1)))
    assert refused.value.status == 413


def test_a_refused_long_request_spends_nothing_and_reaches_nothing() -> None:
    rig = Rig()
    status, _, _ = rig.send(_request(_body(input="x" * SHORT_CONTEXT_TOKENS)))
    assert status == 413 and rig.upstream.calls == [] and rig.gateway.budget.requests == 0 and rig.journal == []
