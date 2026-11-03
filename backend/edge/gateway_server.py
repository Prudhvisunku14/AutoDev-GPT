"""The model gateway's byte mover. Run as a script inside its own container, listening on the internal
model network. The rules are in gateway_policy.py, and the HTTP framing is the API filter's.

The upstream is fixed to https://api.openai.com in `TlsUpstream`, with certificates verified. There is
no setting for it. `http.client` follows no redirect and ignores proxy variables. Offline tests hand
`Gateway` an in-memory `Upstream` instead, so no test can reach OpenAI.
"""

from __future__ import annotations

import argparse
import http.client
import itertools
import json
import os
import socket
import ssl
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

if not __package__:  # run as a script from a read-only mount, as <mount>/edge/gateway_server.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from edge.filter_policy import MAX_RESPONSE_BYTES, MIN_TOKEN_CHARS, Refused, audit_target, authenticated, check_framing, parse_head  # noqa: E402
from edge.filter_server import BUSY_RECORD, CLIENT_SECONDS, STREAM_IDLE_SECONDS, STREAM_MAX_SECONDS, JsonLines, UpstreamReply, build_server, read_body, read_head, reply_error, send_answer  # noqa: E402
from edge.gateway_policy import (  # noqa: E402
    MAX_BODY_BYTES,
    RESPONSES_PATH,
    TOKEN_ENV,
    UPSTREAM_KEY_ENV,
    Budget,
    CheckedRequest,
    GatewayPolicy,
    Reservation,
    Usage,
    UsageScanner,
    answer_status,
    answer_to_429,
    check_request,
    check_route,
    error_facts,
    safe_headers,
    usage_from,
    with_output_limit,
)

UPSTREAM_HOST = "api.openai.com"
UPSTREAM_PORT = 443
_MODEL_SECONDS = 300.0  # a non-streaming answer can take this long


class Upstream(Protocol):
    def send(self, body: bytes, *, stream: bool) -> UpstreamReply: ...


class _TlsReply:
    def __init__(self, connection: http.client.HTTPSConnection, response: http.client.HTTPResponse) -> None:
        self._connection = connection
        self._response = response
        self.status = response.status
        self.media = response.getheader("content-type") or ""
        self.headers = safe_headers(dict(response.getheaders()))  # only the allowed few: never an authorization or cookie header

    def read(self, amount: int) -> bytes:
        return self._response.read1(amount)

    def close(self) -> None:
        self._connection.close()


class TlsUpstream:
    """OpenAI, with the key. A new connection and fixed headers for each request."""

    def __init__(self, key: str) -> None:
        self._authorization = f"Bearer {key}"
        self._context = ssl.create_default_context()

    def send(self, body: bytes, *, stream: bool) -> UpstreamReply:
        timeout = STREAM_IDLE_SECONDS if stream else _MODEL_SECONDS
        connection = http.client.HTTPSConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=timeout, context=self._context)
        headers = {"Authorization": self._authorization, "Content-Type": "application/json", "Accept": "application/json, text/event-stream", "Connection": "close"}
        try:
            connection.request("POST", RESPONSES_PATH, body=body, headers=headers)
            return _TlsReply(connection, connection.getresponse())
        except BaseException:
            connection.close()
            raise


class Gateway:
    def __init__(
        self,
        policy: GatewayPolicy,
        token: str,
        upstream: Upstream,
        *,
        audit: Callable[[dict[str, Any]], None],
        ledger: Callable[[dict[str, Any]], None],
    ) -> None:
        self._policy = policy
        self._token = token
        self._upstream = upstream
        self._audit = audit
        self._ledger = ledger
        self._numbers = itertools.count(1)
        self.budget = Budget(policy)

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
        length = check_framing(head, max_body=MAX_BODY_BYTES)
        if not authenticated(head, self._token):
            raise Refused(401, "the gateway token is missing or wrong")
        check_route(head)
        request = check_request(head, read_body(conn, rest, length), self._policy)
        reservation = self.budget.reserve(request)
        self._forward(conn, request, reservation, record)

    def _forward(self, conn: socket.socket, request: CheckedRequest, reservation: Reservation, record: dict[str, Any]) -> None:
        record["forwarded"] = True
        usage: Usage | None = None
        status = 502
        number = next(self._numbers)
        # On disk before anything leaves: a gateway that dies now can write nothing more, and this line then
        # charges the whole reservation. The line below replaces it (launcher.ledger_rows).
        self._ledger(
            {
                "request": number, "ts": round(time.time(), 3), "model": self._policy.model, "status": 0, "input_tokens": None, "output_tokens": None, "cached_tokens": None,
                "charged_tokens": reservation.tokens, "cost_usd": reservation.usd if self._policy.cost_usd(0, 0) is not None else None, "usage_known": False,
            }
        )  # fmt: skip
        try:
            body = with_output_limit(request, reservation.max_output_tokens)
            try:
                reply = self._upstream.send(body, stream=request.stream)
            except TimeoutError as exc:
                raise Refused(504, "the upstream did not answer in time") from exc
            except (OSError, http.client.HTTPException) as exc:
                raise Refused(502, f"the upstream could not be reached ({type(exc).__name__})") from exc
            try:
                headers = safe_headers(getattr(reply, "headers", {}))  # filtered again here, whatever sent them
                record["upstream"] = {"status": reply.status, **({"headers": headers} if headers else {})}  # why it failed, for whoever reads the audit
                if reply.status == 200:
                    status = answer_status(reply.status, reply.media, stream=request.stream)
                    record["status"] = status
                    if request.stream:
                        usage = _relay(conn, reply)
                    else:
                        data = _read_bounded(reply)
                        usage = _usage_of(data)
                        send_answer(conn, status, "application/json", data)
                else:
                    data = _read_bounded(reply)
                    facts = error_facts(data)
                    record["upstream"].update(facts)
                    if reply.status == 429 and reply.media.split(";", 1)[0].strip().lower() == "application/json":
                        # Rejected before any work, so not billed. Passed on for OpenCode's own bounded backoff, or ended.
                        status, own, extra = answer_to_429(facts, headers)
                        record["status"] = status
                        send_answer(conn, status, "application/json", own if own is not None else data, extra)
                        usage = Usage(0, 0)
                    else:
                        status = answer_status(reply.status, reply.media, stream=request.stream)
                        record["status"] = status
                        send_answer(conn, status, "application/json", data)
            finally:
                reply.close()
        finally:
            charge = self.budget.settle(reservation, usage)
            self._ledger(
                {
                    "request": number, "ts": round(time.time(), 3), "model": self._policy.model, "status": status,
                    "input_tokens": usage.input_tokens if usage else None, "output_tokens": usage.output_tokens if usage else None,
                    "cached_tokens": usage.cached_tokens if usage else None, "charged_tokens": charge.tokens,
                    "cost_usd": charge.usd, "usage_known": charge.known,
                }
            )  # fmt: skip


def _usage_of(data: bytes) -> Usage | None:
    try:
        return usage_from(json.loads(data))
    except ValueError:
        return None


def _read_bounded(reply: UpstreamReply) -> bytes:
    data = bytearray()
    while True:
        chunk = reply.read(65536)
        if not chunk:
            return bytes(data)
        data.extend(chunk)
        if len(data) > MAX_RESPONSE_BYTES:
            raise Refused(502, "the upstream answer is too large")


def _relay(conn: socket.socket, reply: UpstreamReply) -> Usage | None:
    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n")
    scanner = UsageScanner()
    deadline = time.monotonic() + STREAM_MAX_SECONDS
    while time.monotonic() < deadline:
        try:
            chunk = reply.read(65536)
        except TimeoutError:
            break  # a silent model: the client sees the stream end, and the charge stays at the reservation
        if not chunk:
            break
        scanner.feed(chunk)
        conn.sendall(chunk)
    return scanner.usage


def load_policy(path: Path) -> GatewayPolicy:
    return GatewayPolicy.from_config(json.loads(path.read_text(encoding="utf-8")))


def credentials(environ: dict[str, str] | os._Environ[str]) -> tuple[str, str]:
    token, key = environ.get(TOKEN_ENV, ""), environ.get(UPSTREAM_KEY_ENV, "")
    if len(token) < MIN_TOKEN_CHARS:
        raise ValueError(f"{TOKEN_ENV} is missing or shorter than {MIN_TOKEN_CHARS} characters")
    if not key:
        raise ValueError(f"{UPSTREAM_KEY_ENV} is missing")
    return token, key


def main() -> int:
    parser = argparse.ArgumentParser(description="DevPilot's model gateway in front of OpenAI")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=8081)
    parser.add_argument("--audit", required=True, type=Path)
    parser.add_argument("--ledger", required=True, type=Path)
    args = parser.parse_args()
    try:
        policy = load_policy(args.config)
        token, key = credentials(os.environ)
    except (OSError, ValueError) as exc:
        print(f"the gateway did not start: {exc}", file=sys.stderr)
        return 2
    with args.audit.open("a", encoding="utf-8", buffering=1) as audit, args.ledger.open("a", encoding="utf-8", buffering=1) as ledger:
        server = build_server((args.listen_host, args.listen_port), Gateway(policy, token, TlsUpstream(key), audit=JsonLines(audit), ledger=JsonLines(ledger)))
        with server:
            server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
