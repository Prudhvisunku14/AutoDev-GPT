"""The API filter's rules: what may reach OpenCode, and in exactly what shape (Phase 4D Stage 7).

OpenCode 1.19.5 has 188 operations, including interactive terminals, shell commands and configuration
changes, and it answers unknown paths with 200 and its web page. DevPilot needs 14 of them. The filter
is the only route to the server, and everything not listed in `OPERATIONS` is refused.

This module decides and filter_server.py moves bytes. Both use only the standard library, because they
run in a container with nothing installed. Every function here is pure, so each rule can be tested
without a network.

Checks run in this order: framing, authentication, route, body. A refused request never reaches the
upstream, and an unauthenticated caller learns nothing about which paths exist.

Nothing a client sends is forwarded as it arrived. The filter builds a new request from a fixed path,
fixed headers and a body it has parsed and serialized again, on a new connection, one request per
connection. So a difference between how the filter and OpenCode would read a request has nothing to
act on: there is no raw text left to read differently. The strict parsing below refuses ambiguous
requests anyway, so a client that sends one is told, and the attempt is logged.

The request shapes come from the server's own OpenAPI document. They are narrower than what the server
accepts, because each field DevPilot does not need is a way to change what the server does:
- `POST /session` also takes `agent`, `model`, `parentID`, `metadata` and `workspaceID`.
- `prompt_async` also takes `tools`, which replaces the session's whole rule list, as well as `system`,
  `noReply`, `format` and `variant`, and file, agent and subtask parts.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

FILTER_SERVICE = "devpilot-api-filter"
HEALTH_PATH = "/devpilot/health"
TOKEN_ENV = "DEVPILOT_FILTER_TOKEN"
PASSWORD_ENV = "OPENCODE_SERVER_PASSWORD"
MIN_TOKEN_CHARS = 32
PROMPTED_AGENT = "build"

MAX_HEAD_BYTES = 16 * 1024
MAX_HEADERS = 48
MAX_REQUEST_BYTES = 128 * 1024
MAX_BRIEF_CHARS = 64 * 1024
MAX_TITLE_CHARS = 200
MAX_REPLY_CHARS = 200
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_EVENT_BYTES = 8 * 1024 * 1024
_MAX_JSON_DEPTH = 32

_TOKEN = re.compile(rb"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")  # RFC 9110 `token`: a method or a header name
_REQUEST_LINE = re.compile(rb"^([!#$%&'*+\-.^_`|~0-9A-Za-z]+) ([\x21-\x7e]+) HTTP/1\.1$")
_LENGTH = re.compile(r"^[0-9]{1,9}$")
_MESSAGE_ID = re.compile(r"^msg_[0-9a-f]{12}[0-9A-Za-z]{14}$")
_ID_SHAPES = {"{session}": "ses_[A-Za-z0-9]{1,64}", "{permission}": "per_[A-Za-z0-9]{1,64}", "{question}": "que_[A-Za-z0-9]{1,64}"}
_REFUSED_HEADERS = frozenset(
    {"transfer-encoding", "te", "trailer", "upgrade", "expect", "x-http-method-override", "x-http-method", "x-method-override",
     "x-original-url", "x-original-method", "x-rewrite-url", "x-forwarded-path"}
)  # fmt: skip
_NO_SUCH_ROUTE = "this operation is not allowed"
_EVENT_BREAK = re.compile(rb"\r\n\r\n|\n\n")


class Refused(Exception):
    """The request stops here. `reason` is for the audit log; nothing here holds a secret."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


@dataclass(frozen=True)
class Head:
    method: str
    target: str
    headers: tuple[tuple[str, str], ...]  # names in lower case, in the order they arrived

    def values(self, name: str) -> list[str]:
        return [value for key, value in self.headers if key == name]


@dataclass(frozen=True)
class Operation:
    method: str
    template: str
    scoped: bool = True  # must carry exactly the approved `directory`
    body: str = "none"  # none, session, prompt or reply
    answer: str = "json"  # json, accepted, stream or local

    def pattern(self) -> re.Pattern[str]:
        text = re.escape(self.template)
        for name, shape in _ID_SHAPES.items():
            text = text.replace(re.escape(name), shape)
        return re.compile(text)


OPERATIONS: tuple[Operation, ...] = (
    Operation("GET", HEALTH_PATH, scoped=False, answer="local"),
    Operation("GET", "/global/health", scoped=False),
    Operation("GET", "/doc", scoped=False),
    Operation("GET", "/config"),
    Operation("GET", "/agent"),
    Operation("GET", "/experimental/tool/ids"),
    Operation("GET", "/session/status"),
    Operation("GET", "/session/{session}"),
    Operation("GET", "/session/{session}/message"),
    Operation("GET", "/event", answer="stream"),
    Operation("POST", "/session", body="session"),
    Operation("POST", "/session/{session}/prompt_async", body="prompt", answer="accepted"),
    Operation("POST", "/session/{session}/abort"),
    Operation("POST", "/permission/{permission}/reply", body="reply"),
    Operation("POST", "/question/{question}/reject"),
)
_ROUTES = tuple((operation, operation.pattern()) for operation in OPERATIONS)


@dataclass(frozen=True)
class Policy:
    """What this one run may do. Written by the launcher, one per run."""

    directory: str
    provider_id: str
    model_id: str
    session_rules: tuple[Any, ...]

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> Policy:
        directory = config.get("directory")
        rules = config.get("session_rules")
        provider, model = config.get("provider_id"), config.get("model_id")
        if not isinstance(directory, str) or not directory.startswith("/") or ".." in directory:
            raise ValueError("the configuration has no usable directory")
        if not (isinstance(provider, str) and provider and isinstance(model, str) and model):
            raise ValueError("the configuration has no approved model")
        if not isinstance(rules, list) or not rules:
            raise ValueError("the configuration has no session rules")
        return cls(directory=directory, provider_id=provider, model_id=model, session_rules=tuple(rules))

    def scoped_queries(self) -> frozenset[str]:
        return frozenset({f"directory={quote(self.directory, safe='')}", f"directory={self.directory}"})

    def upstream_query(self) -> str:
        return f"directory={quote(self.directory, safe='')}"


@dataclass(frozen=True)
class Route:
    operation: Operation
    path: str
    upstream_target: str


# --- Framing ----------------------------------------------------------------------------------------


def parse_head(raw: bytes) -> Head:
    """The request line and headers, from everything up to and including the blank line.

    Stricter than HTTP allows a server to be: CRLF only, no folding, no spaces around a header name.
    A request that two parsers could read differently is refused rather than interpreted."""
    if not raw.endswith(b"\r\n\r\n"):
        raise Refused(400, "the request head is not terminated")
    lines = raw[:-4].split(b"\r\n")
    if any(b"\n" in line or b"\r" in line or b"\x00" in line for line in lines):
        raise Refused(400, "a stray line ending or NUL in the request head")
    match = _REQUEST_LINE.fullmatch(lines[0])
    if match is None:
        raise Refused(400, "the request line is not HTTP/1.1 in origin form")
    if len(lines) - 1 > MAX_HEADERS:
        raise Refused(431, "too many headers")
    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        if line[:1] in (b" ", b"\t"):
            raise Refused(400, "a folded header")
        name, separator, value = line.partition(b":")
        if not separator or not _TOKEN.fullmatch(name):
            raise Refused(400, "a malformed header name")
        value = value.strip(b" \t")
        if any(byte < 0x20 and byte != 0x09 or byte == 0x7f for byte in value):
            raise Refused(400, "a control character in a header value")
        headers.append((name.decode("ascii").lower(), value.decode("latin-1")))
    return Head(match[1].decode("ascii"), match[2].decode("ascii"), tuple(headers))


def check_framing(head: Head, *, max_body: int = MAX_REQUEST_BYTES) -> int:
    """How long the body is. Anything that could frame the request two ways is refused."""
    found = sorted({name for name, _ in head.headers} & _REFUSED_HEADERS)
    if found:
        raise Refused(400, f"the header {found[0]} is not accepted")
    if len(head.values("host")) != 1:
        raise Refused(400, "exactly one Host header is required")
    for value in head.values("connection"):
        if value.strip().lower() not in ("close", "keep-alive"):
            raise Refused(400, "the Connection header is not plain close or keep-alive")
    lengths = head.values("content-length")
    if len(lengths) > 1:
        raise Refused(400, "more than one Content-Length")
    if not lengths:
        return 0
    if not _LENGTH.fullmatch(lengths[0]):
        raise Refused(400, "a malformed Content-Length")
    length = int(lengths[0])
    if length > max_body:
        raise Refused(413, "the request body is too large")
    return length


# --- Authentication ---------------------------------------------------------------------------------


def authenticated(head: Head, token: str) -> bool:
    """A fresh per-run token, compared in constant time over digests, so length leaks nothing either."""
    values = head.values("authorization")
    presented = values[0] if len(values) == 1 else ""
    candidate = presented[7:] if presented.startswith("Bearer ") else ""
    expected = hashlib.sha256(token.encode("utf-8")).digest()
    given = hashlib.sha256(candidate.encode("latin-1")).digest()
    # Evaluated in full either way: an empty token can never match, and no branch depends on the secret.
    return hmac.compare_digest(given, expected) and bool(candidate) and bool(token)


# --- Route ------------------------------------------------------------------------------------------


def split_target(target: str) -> tuple[str, str]:
    """(path, query), or refused. The paths DevPilot uses never need encoding, so none is decoded."""
    if not target.startswith("/") or target.startswith("//"):
        raise Refused(403, "the target is not a plain path")
    path, separator, query = target.partition("?")
    if separator and not query:
        raise Refused(403, "an empty query")
    if "#" in target or any(ch in path for ch in "%\\;"):
        raise Refused(403, "encoding or a parameter in the path")
    if path != "/" and any(segment in ("", ".", "..") for segment in path.split("/")[1:]):
        raise Refused(403, "a path that could be normalized")
    return path, query


def find_route(head: Head, policy: Policy) -> Route:
    """The one allowed operation this request is, or refused. Unknown methods and paths get the same answer."""
    path, query = split_target(head.target)
    for operation, pattern in _ROUTES:
        if operation.method == head.method and pattern.fullmatch(path):
            if operation.scoped and query not in policy.scoped_queries():
                raise Refused(403, "the query must be exactly the approved directory")
            if not operation.scoped and query:
                raise Refused(403, "this operation takes no query")
            target = f"{path}?{policy.upstream_query()}" if operation.scoped else path
            return Route(operation, path, target)
    raise Refused(403, _NO_SUCH_ROUTE)


# --- Bodies -----------------------------------------------------------------------------------------


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise Refused(403, "a duplicate field in the body")
    return dict(pairs)


def _refuse_constant(_: str) -> None:
    raise Refused(403, "a value JSON does not define")


def strict_json(body: bytes) -> Any:
    try:
        text = body.decode("utf-8")
        return json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_refuse_constant)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise Refused(400, "the body is not valid JSON") from exc


def _fields(payload: Any, *, required: set[str], optional: frozenset[str] = frozenset()) -> Mapping[str, Any]:
    if not isinstance(payload, dict):
        raise Refused(403, "the body is not an object")
    extra = sorted(set(payload) - required - optional)
    if extra:
        raise Refused(403, f"the field {extra[0][:40]!r} is not allowed")
    missing = sorted(required - set(payload))
    if missing:
        raise Refused(403, f"the field {missing[0]!r} is required")
    return payload


def _plain_text(value: Any, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value:
        raise Refused(403, f"{name} must be text")
    if len(value) > limit:
        raise Refused(403, f"{name} is too long")
    return value


def _check_session(payload: Any, policy: Policy) -> dict[str, Any]:
    fields = _fields(payload, required={"title", "permission"})
    title = _plain_text(fields["title"], "the title", MAX_TITLE_CHARS)
    if any(ord(ch) < 0x20 for ch in title):
        raise Refused(403, "the title has a control character")
    if tuple(fields["permission"] if isinstance(fields["permission"], list) else ()) != policy.session_rules:
        raise Refused(403, "the rule list is not the approved one")
    return {"title": title, "permission": list(policy.session_rules)}


def _check_prompt(payload: Any, policy: Policy) -> dict[str, Any]:
    fields = _fields(payload, required={"agent", "model", "parts"}, optional=frozenset({"messageID"}))
    if fields["agent"] != PROMPTED_AGENT:
        raise Refused(403, "only the build agent may be prompted")
    model = {"providerID": policy.provider_id, "modelID": policy.model_id}
    if fields["model"] != model:
        raise Refused(403, "only the approved model may be used")
    parts = fields["parts"]
    if not isinstance(parts, list) or len(parts) != 1 or not isinstance(parts[0], dict):
        raise Refused(403, "the prompt must be exactly one text part")
    if parts[0].get("type") != "text":
        raise Refused(403, "only text parts are allowed")
    part = _fields(parts[0], required={"type", "text"})
    clean: dict[str, Any] = {"agent": PROMPTED_AGENT, "model": model,
                             "parts": [{"type": "text", "text": _plain_text(part["text"], "the prompt", MAX_BRIEF_CHARS)}]}  # fmt: skip
    if "messageID" in fields:
        if not isinstance(fields["messageID"], str) or not _MESSAGE_ID.fullmatch(fields["messageID"]):
            raise Refused(403, "the message ID does not have the server's shape")
        clean["messageID"] = fields["messageID"]
    return clean


def _check_reply(payload: Any) -> dict[str, Any]:
    fields = _fields(payload, required={"reply"}, optional=frozenset({"message"}))
    if fields["reply"] != "reject":
        raise Refused(403, "a permission can only be rejected")
    clean: dict[str, Any] = {"reply": "reject"}
    if "message" in fields:
        clean["message"] = _plain_text(fields["message"], "the message", MAX_REPLY_CHARS)
    return clean


def check_body(operation: Operation, head: Head, body: bytes, policy: Policy) -> bytes | None:
    """The body to forward, rebuilt from what was checked, or None for an operation that takes none."""
    if operation.body == "none":
        if body:
            raise Refused(403, "this operation takes no body")
        return None
    media = (head.values("content-type") or [""])[0].split(";", 1)[0].strip().lower()
    if len(head.values("content-type")) != 1 or media != "application/json":
        raise Refused(403, "the body must be declared as application/json")
    payload = strict_json(body)
    if json_depth(payload) > _MAX_JSON_DEPTH:
        raise Refused(403, "the body is nested too deeply")
    if operation.body == "session":
        clean = _check_session(payload, policy)
    elif operation.body == "prompt":
        clean = _check_prompt(payload, policy)
    else:
        clean = _check_reply(payload)
    return json.dumps(clean, ensure_ascii=True, separators=(",", ":")).encode("ascii")


def json_depth(value: Any) -> int:
    """How deeply a value nests. Iterative: nesting is what it measures, so it must not recurse on it."""
    deepest = 0
    pending = [(value, 1)]
    while pending:
        item, level = pending.pop()
        children = item.values() if isinstance(item, dict) else item if isinstance(item, list) else None
        if children is not None:
            deepest = max(deepest, level)
            pending.extend((child, level + 1) for child in children)
    return deepest


# --- Answers ----------------------------------------------------------------------------------------


def answer_problem(operation: Operation, status: int, content_type: str) -> str | None:
    """Why this upstream answer must not reach the client. None when it may."""
    media = content_type.split(";", 1)[0].strip().lower()
    if operation.answer == "stream":
        return None if status == 200 and media == "text/event-stream" else f"the event stream answered {status} {media or 'nothing'}"
    if operation.answer == "accepted" and status == 204:
        return None
    if status in (400, 404) and media == "application/json":
        return None
    if operation.answer == "json" and status == 200 and media == "application/json":
        return None
    return f"the upstream answered {status} {media or 'nothing'}"


class EventLimiter:
    """Ends a stream once a single event grows past the limit, without buffering it."""

    def __init__(self, limit: int = MAX_EVENT_BYTES) -> None:
        self._limit = limit
        self._open = 0  # bytes of the event still being received
        self._tail = b""  # a boundary can straddle two chunks

    def accept(self, chunk: bytes) -> bool:
        tail, self._tail = self._tail, chunk[-3:]
        pieces = _EVENT_BREAK.split(tail + chunk)
        if len(pieces) == 1:
            self._open += len(chunk)
            return self._open <= self._limit
        complete = [self._open + len(pieces[0]) - len(tail), *(len(piece) for piece in pieces[1:-1])]
        self._open = len(pieces[-1])
        return max(complete) <= self._limit and self._open <= self._limit


def audit_target(target: str) -> str:
    """The path as far as it is safe to log: never the query, and only printable characters."""
    path = target.partition("?")[0][:200]
    return "".join(ch if "\x20" <= ch <= "\x7e" else "?" for ch in path)
