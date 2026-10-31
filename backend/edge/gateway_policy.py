"""The model gateway's rules: what may reach OpenAI, and how much of it (SECURITY T3, T6).

OpenCode never holds the OpenAI key. It talks to this gateway with a per-run token, and the gateway
adds the key outside OpenCode's container. So a compromised coding session can spend only what this
run allows, on the one approved model, and can never read the key.

The pinned 1.19.5 binary was read to find what OpenCode sends. Its `openai` provider calls
`sdk.responses(...)`, which is `POST {baseURL}/responses`, always streaming. The fields below are the
union of what its two Responses clients write. Anything else is refused, because the fields that are
left out each do something a coding session has no need for:
- `background` would detach the request from the stream the budget is counted on.
- `previous_response_id`, `conversation` and `prompt` read state stored at OpenAI.
- Hosted tools (web search, file search, code interpreter, computer use, MCP) run outside the
  container's network rules, and a URL in an image or file part makes OpenAI fetch it.

This module decides and gateway_server.py moves bytes. Both use only the standard library. Every
function here is pure except `Budget`, which holds the run's counters behind a lock.

Budgets are enforced before forwarding. A request reserves its worst case, so a request that could
overrun is refused, not discovered afterwards. Each answer then replaces the reservation with the
real usage. An answer whose usage cannot be read keeps the whole reservation, so missing numbers
can never make spending invisible.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from edge.filter_policy import MAX_EVENT_BYTES, Head, Refused, json_depth, strict_json

GATEWAY_SERVICE = "devpilot-model-gateway"
TOKEN_ENV = "DEVPILOT_GATEWAY_TOKEN"
UPSTREAM_KEY_ENV = "DEVPILOT_GATEWAY_OPENAI_KEY"
RESPONSES_PATH = "/v1/responses"
MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_TOOLS = 64
_MAX_JSON_DEPTH = 96
_BYTES_PER_TOKEN = 3  # counted low, so the input estimate errs towards refusing

ALLOWED_FIELDS = frozenset(
    {
        "model", "input", "instructions", "tools", "tool_choice", "stream", "max_output_tokens", "store", "service_tier",
        "prompt_cache_key", "prompt_cache_retention", "include", "reasoning", "text", "temperature", "top_p",
        "parallel_tool_calls", "metadata", "truncation", "safety_identifier", "user", "stream_options",
    }
)  # fmt: skip
ALLOWED_INCLUDES = frozenset({"reasoning.encrypted_content", "message.output_text.logprobs"})
ALLOWED_SERVICE_TIERS = frozenset({"auto", "default", "flex"})  # never "priority", which costs more
ALLOWED_ITEM_TYPES = frozenset({"message", "reasoning", "item_reference", "function_call", "function_call_output"})
ALLOWED_PART_TYPES = frozenset({"input_text", "output_text", "input_image", "summary_text", "refusal"})
_TOOL_CHOICES = frozenset({"auto", "none", "required"})
_FETCHED_KEYS = frozenset({"image_url", "file_url"})
_FINAL_EVENTS = (b"response.completed", b"response.incomplete", b"response.failed")
_EVENT_BREAK = re.compile(rb"\r\n\r\n|\n\n")


@dataclass(frozen=True)
class GatewayPolicy:
    """What this one run may spend. Written by the launcher, one per attempt."""

    model: str
    max_output_tokens: int
    max_requests: int
    max_tokens: int
    max_usd: float | None = None
    price_input_per_mtok: float | None = None
    price_output_per_mtok: float | None = None

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> GatewayPolicy:
        model = config.get("model")
        if not isinstance(model, str) or not model or "/" in model:
            raise ValueError("the configuration has no approved model")
        for name in ("max_output_tokens", "max_requests", "max_tokens"):
            value = config.get(name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"the configuration has no usable {name}")
        prices = (config.get("price_input_per_mtok"), config.get("price_output_per_mtok"))
        if (prices[0] is None) != (prices[1] is None):
            raise ValueError("the configuration has only one of the two prices")
        if config.get("max_usd") is not None and prices[0] is None:
            raise ValueError("a spending limit needs both prices")
        return cls(model, config["max_output_tokens"], config["max_requests"], config["max_tokens"], config.get("max_usd"), prices[0], prices[1])

    def cost_usd(self, input_tokens: int, output_tokens: int) -> float | None:
        if self.price_input_per_mtok is None or self.price_output_per_mtok is None:
            return None
        return (input_tokens * self.price_input_per_mtok + output_tokens * self.price_output_per_mtok) / 1_000_000


@dataclass(frozen=True)
class CheckedRequest:
    body: bytes  # rebuilt from the parsed fields, with the output cap in it
    stream: bool
    input_estimate: int
    max_output_tokens: int


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int
    cached_tokens: int = 0


def check_route(head: Head) -> None:
    """One operation exists. There is no query, because the upstream target is fixed."""
    if head.method != "POST" or head.target != RESPONSES_PATH:
        raise Refused(403, "this operation is not allowed")


def check_request(head: Head, body: bytes, policy: GatewayPolicy) -> CheckedRequest:
    media = (head.values("content-type") or [""])[0].split(";", 1)[0].strip().lower()
    if len(head.values("content-type")) != 1 or media != "application/json":
        raise Refused(403, "the body must be declared as application/json")
    payload = strict_json(body)
    if not isinstance(payload, dict):
        raise Refused(403, "the body is not an object")
    if json_depth(payload) > _MAX_JSON_DEPTH:
        raise Refused(403, "the body is nested too deeply")
    extra = sorted(set(payload) - ALLOWED_FIELDS)
    if extra:
        raise Refused(403, f"the field {extra[0][:40]!r} is not allowed")
    if payload.get("model") != policy.model:
        raise Refused(403, "only the approved model may be used")
    if not isinstance(payload.get("input"), (str, list)) or not payload["input"]:
        raise Refused(403, "the request has no input")
    _check_tools(payload.get("tools"), payload.get("tool_choice"))
    _check_items(payload["input"])
    _check_options(payload)
    asked = payload.get("max_output_tokens", policy.max_output_tokens)
    if not isinstance(asked, int) or isinstance(asked, bool) or asked <= 0:
        raise Refused(403, "max_output_tokens is not a positive number")
    stream = payload.get("stream", False)
    if not isinstance(stream, bool):
        raise Refused(403, "stream is not true or false")
    limited = min(asked, policy.max_output_tokens)
    clean = {**payload, "max_output_tokens": limited}
    return CheckedRequest(_serialized(clean), stream, len(body) // _BYTES_PER_TOKEN, limited)


def _check_tools(tools: Any, choice: Any) -> None:
    if tools is not None:
        if not isinstance(tools, list) or len(tools) > MAX_TOOLS:
            raise Refused(403, "tools is not a list of allowed size")
        for tool in tools:
            if not isinstance(tool, dict) or tool.get("type") != "function":
                raise Refused(403, "only function tools are allowed; hosted tools are refused")
    if choice is None or (isinstance(choice, str) and choice in _TOOL_CHOICES):
        return
    if isinstance(choice, dict) and choice.get("type") == "function" and isinstance(choice.get("name"), str) and set(choice) == {"type", "name"}:
        return
    raise Refused(403, "tool_choice may not name a hosted tool")


def _check_items(value: Any) -> None:
    """Every item and content part is of a known kind, and nothing makes OpenAI fetch a URL."""
    if isinstance(value, str):
        return
    for item in value:
        if not isinstance(item, dict):
            raise Refused(403, "an input item is not an object")
        kind = item.get("type", "message")
        if kind not in ALLOWED_ITEM_TYPES:
            raise Refused(403, f"the input item type {str(kind)[:40]!r} is not allowed")
        content = item.get("content")
        for part in content if isinstance(content, list) else []:
            if not isinstance(part, dict) or part.get("type") not in ALLOWED_PART_TYPES:
                raise Refused(403, "an input part is not an allowed type")
    for key, text in _strings(value):
        if key in _FETCHED_KEYS and not text.startswith("data:"):
            raise Refused(403, "a URL in the input would make the provider fetch it")


def _strings(value: Any) -> Iterable[tuple[str, str]]:
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, str):
                yield str(key), item
            else:
                yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _check_options(payload: Mapping[str, Any]) -> None:
    include = payload.get("include")
    if include is not None and (not isinstance(include, list) or not set(map(str, include)) <= ALLOWED_INCLUDES):
        raise Refused(403, "include asks for something that is not allowed")
    tier = payload.get("service_tier")
    if tier is not None and tier not in ALLOWED_SERVICE_TIERS:
        raise Refused(403, "this service tier is not allowed")


def _serialized(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii")


class Budget:
    """The run's counters. `reserve` refuses before anything is forwarded."""

    def __init__(self, policy: GatewayPolicy) -> None:
        self._policy = policy
        self._lock = threading.Lock()
        self.requests = 0
        self.tokens = 0
        self.usd = 0.0
        self._held = 0  # tokens reserved by requests still running
        self._held_usd = 0.0

    def reserve(self, request: CheckedRequest) -> Reservation:
        policy = self._policy
        with self._lock:
            if self.requests >= policy.max_requests:
                raise Refused(429, "the request budget is used up")
            room = policy.max_tokens - self.tokens - self._held - request.input_estimate
            if room <= 0:
                raise Refused(429, "the token budget is used up")
            output = min(request.max_output_tokens, room)
            worst = policy.cost_usd(request.input_estimate, output)
            if policy.max_usd is not None and worst is not None and self.usd + self._held_usd + worst > policy.max_usd:
                raise Refused(429, "the spending budget is used up")
            self.requests += 1
            held = request.input_estimate + output
            self._held += held
            self._held_usd += worst or 0.0
            return Reservation(tokens=held, usd=worst or 0.0, max_output_tokens=output)

    def settle(self, reservation: Reservation, usage: Usage | None) -> Charge:
        """Replace a reservation with what was really used. Unknown usage keeps the whole reservation."""
        with self._lock:
            self._held -= reservation.tokens
            self._held_usd -= reservation.usd
            if usage is None:
                self.tokens += reservation.tokens
                self.usd += reservation.usd
                return Charge(reservation.tokens, reservation.usd if self._policy.cost_usd(0, 0) is not None else None, known=False)
            spent = usage.input_tokens + usage.output_tokens
            cost = self._policy.cost_usd(usage.input_tokens, usage.output_tokens)
            self.tokens += spent
            self.usd += cost or 0.0
            return Charge(spent, cost, known=True)


@dataclass(frozen=True)
class Reservation:
    tokens: int
    usd: float
    max_output_tokens: int


@dataclass(frozen=True)
class Charge:
    tokens: int
    usd: float | None
    known: bool  # False: the usage could not be read, so the reservation was charged


def with_output_limit(request: CheckedRequest, limit: int) -> bytes:
    """The body again, when the budget left less room than the request's own cap."""
    if limit == request.max_output_tokens:
        return request.body
    return _serialized({**json.loads(request.body), "max_output_tokens": limit})


def usage_from(payload: Any) -> Usage | None:
    """The usage object of a Responses answer, or None unless both numbers are really there."""
    if not isinstance(payload, dict):
        return None
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    numbers = usage.get("input_tokens"), usage.get("output_tokens")
    if not all(isinstance(n, int) and not isinstance(n, bool) and n >= 0 for n in numbers):
        return None
    details = usage.get("input_tokens_details")
    cached = details.get("cached_tokens") if isinstance(details, dict) else 0
    return Usage(numbers[0], numbers[1], cached if isinstance(cached, int) and not isinstance(cached, bool) else 0)


class UsageScanner:
    """Finds the final usage in a Responses stream, holding at most one event at a time."""

    def __init__(self, limit: int = MAX_EVENT_BYTES) -> None:
        self._limit = limit
        self._buffer = bytearray()
        self.usage: Usage | None = None

    def feed(self, chunk: bytes) -> None:
        self._buffer.extend(chunk)
        while True:
            found = _EVENT_BREAK.search(self._buffer)
            if found is None:
                break
            event = bytes(self._buffer[: found.start()])
            del self._buffer[: found.end()]
            self._read(event)
        if len(self._buffer) > self._limit:  # an event this size is dropped, and the charge stays at the reservation
            self._buffer.clear()

    def _read(self, event: bytes) -> None:
        if not any(name in event for name in _FINAL_EVENTS):
            return
        lines = [line[5:].strip() for line in event.splitlines() if line.startswith(b"data:")]
        try:
            payload = json.loads(b"\n".join(lines))
        except ValueError:
            return
        if isinstance(payload, dict) and isinstance(payload.get("response"), dict):
            self.usage = usage_from(payload["response"]) or self.usage


def answer_status(status: int, media: str, *, stream: bool) -> int:
    """The status the client may see for an upstream answer. A problem with DevPilot's own key is never
    passed on, and neither is a redirect, because the upstream target is fixed."""
    kind = media.split(";", 1)[0].strip().lower()
    if status == 200 and kind == ("text/event-stream" if stream else "application/json"):
        return 200
    if status in (400, 404, 409, 413, 422, 429) and kind == "application/json":
        return status
    raise Refused(502, f"the upstream answered {status} {kind or 'nothing'}")
