"""OpenCode API contract checks: pure functions, tested without any server.

These are the checks the HTTP adapter needs before it may talk to a server. The adapter
(tools/adapters/opencode.py, untested and uncommitted) does not use this module yet.
Every check fails closed: anything unexpected stops the task before a prompt is sent.

Sources: the published opencode-ai 1.19.5 SDK types and the server docs (checked
2026-10-30). Nothing here has been confirmed against a running server yet; see
docs/ARCHITECTURE_OPTIMIZATION.md for which parts are verified, inferred or unknown.
"""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from app.config import Settings
from app.core.egress import EgressPolicy

CONTRACT_VERSION = "1.19.5"
# 1.19.5 is the release GitHub marks Latest and npm's `latest`. A 2.x line exists on GitHub
# and GHCR with changed permission checks and a new configuration layer: refused until verified.
SUPPORTED_VERSIONS = ((1, 19, 0), (2, 0, 0))  # from (inclusive) to (exclusive)

REQUIRED_OPERATIONS = frozenset(
    {
        ("get", "/global/health"),
        ("get", "/event"),
        ("get", "/config"),
        ("post", "/session"),
        ("get", "/session/{}"),
        ("get", "/session/status"),
        ("post", "/session/{}/prompt_async"),
        ("post", "/session/{}/abort"),
        ("get", "/session/{}/message"),
    }
)
# The SDK replies through the first; the docs describe the second. Whichever the server lists is used.
PERMISSION_REPLY_OPERATIONS = (("post", "/permission/{}/reply"), ("post", "/session/{}/permissions/{}"))
QUESTION_REJECT_OPERATION = ("post", "/question/{}/reject")
# Operations that run commands or change the server. DevPilot never calls them, and the deployment
# must stop anyone else from reaching them (ARCHITECTURE §6).
SENSITIVE_OPERATIONS = frozenset(
    {
        ("post", "/session/{}/shell"),
        ("post", "/session/{}/command"),
        ("patch", "/config"),
        ("put", "/auth/{}"),
    }
)
_SENSITIVE_PREFIXES = ("/mcp", "/tui", "/pty", "/experimental", "/instance")
_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete"})
_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")

# Tools the build agent needs to read and edit files. Any other tool that runs means the policy
# did not hold (a newer OpenCode adding a tool is treated the same way until reviewed).
ALLOWED_TOOLS = frozenset({"read", "write", "edit", "patch", "multiedit", "glob", "grep", "list", "todowrite", "todoread"})


class ContractError(Exception):
    """The server cannot be used. `kind` is incompatible, unavailable or protocol."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclass(frozen=True)
class Contract:
    version: str
    permission_reply: str  # normalized path template of the reply operation
    question_reject: str | None
    sensitive: tuple[str, ...]  # sensitive operations the server exposes, for the security inventory


def parse_version(text: str) -> tuple[int, int, int] | None:
    match = _VERSION.match(text.strip())
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


def check_health(payload: Any) -> str:
    """The version from `GET /global/health`, or ContractError."""
    if not isinstance(payload, dict) or payload.get("healthy") is not True:
        raise ContractError("unavailable", "OpenCode reports that it is not healthy")
    version = str(payload.get("version", ""))
    parsed = parse_version(version)
    low, high = SUPPORTED_VERSIONS
    if parsed is None or not low <= parsed < high:
        raise ContractError(
            "incompatible",
            f"OpenCode version {version or 'unknown'} is not supported "
            f"(supported: {_dotted(low)} up to, not including, {_dotted(high)})",
        )
    return version


def spec_from_doc(content_type: str, body: bytes) -> dict[str, Any]:
    """The OpenAPI document from `GET /doc`. Only a JSON document is accepted.

    The server docs describe `/doc` as an HTML page containing the spec. DevPilot does not
    scrape HTML: until the JSON location is confirmed on a real server, an HTML answer
    makes the server incompatible.
    """
    media = content_type.split(";", 1)[0].strip().lower()
    if media in ("text/html", "application/xhtml+xml") or body.lstrip()[:1] == b"<":
        raise ContractError(
            "incompatible",
            "GET /doc returned an HTML page, not a JSON OpenAPI document; "
            "the JSON spec location for this OpenCode version has not been confirmed",
        )
    try:
        spec = json.loads(body)
    except ValueError as exc:
        raise ContractError("protocol", f"GET /doc returned {media or 'an unknown type'} that is not JSON") from exc
    if not isinstance(spec, dict) or not isinstance(spec.get("paths"), dict):
        raise ContractError("incompatible", "GET /doc did not return an OpenAPI document with paths")
    return spec


def operations(spec: dict[str, Any]) -> set[tuple[str, str]]:
    paths = spec.get("paths")
    if not isinstance(paths, dict):
        return set()
    return {
        (method.lower(), template(path))
        for path, item in paths.items()
        if isinstance(path, str) and isinstance(item, dict)
        for method in item
        if isinstance(method, str) and method.lower() in _HTTP_METHODS
    }


def check_spec(spec: dict[str, Any], version: str) -> Contract:
    ops = operations(spec)
    missing = sorted(f"{m.upper()} {p}" for m, p in REQUIRED_OPERATIONS - ops)
    if missing:
        raise ContractError("incompatible", f"the OpenCode API lacks operations DevPilot needs: {', '.join(missing)}")
    reply = next((path for method, path in PERMISSION_REPLY_OPERATIONS if (method, path) in ops), None)
    if reply is None:
        raise ContractError("incompatible", "the OpenCode API has no operation to answer permission requests")
    question = QUESTION_REJECT_OPERATION[1] if QUESTION_REJECT_OPERATION in ops else None
    return Contract(version=version, permission_reply=reply, question_reject=question, sensitive=tuple(sensitive_operations(ops)))


def sensitive_operations(ops: set[tuple[str, str]]) -> list[str]:
    found = {(m, p) for m, p in ops if (m, p) in SENSITIVE_OPERATIONS or p.startswith(_SENSITIVE_PREFIXES)}
    return sorted(f"{m.upper()} {p}" for m, p in found)


def template(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", path).rstrip("/") or "/"


def tool_violation(tool: str, status: str) -> str | None:
    """A message when a tool outside the allowlist starts or finishes, otherwise None."""
    if status in ("running", "completed") and tool not in ALLOWED_TOOLS:
        return f"the engine ran the tool {tool!r}, which DevPilot does not allow"
    return None


def inside_workspace(path: str, workspace: str) -> bool:
    """True when an absolute POSIX path lies strictly inside the workspace folder."""
    root = workspace.rstrip("/")
    if not path.startswith("/") or not root:
        return False
    parts: list[str] = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                return False
            parts.pop()
        else:
            parts.append(part)
    normalized = "/" + "/".join(parts)
    return normalized.startswith(root + "/")


class SSEParser:
    """Server-sent events to JSON objects, with a size cap per event.

    Bytes are fed in whatever chunks arrive. A line or an event larger than `max_event_bytes`
    is dropped (reported as `devpilot.oversized_event`) instead of being buffered.
    """

    def __init__(self, *, max_event_bytes: int = 1_048_576) -> None:
        self._max = max_event_bytes
        self._buffer = bytearray()
        self._data: list[bytes] = []
        self._size = 0
        self._oversized = False
        self.dropped = 0

    def feed(self, chunk: bytes) -> list[dict[str, Any]]:
        self._buffer.extend(chunk)
        events: list[dict[str, Any]] = []
        while True:
            end = self._line_end()
            if end is None:
                if len(self._buffer) > self._max:
                    self._oversized = True
                    self._buffer.clear()
                return events
            line, consumed = end
            del self._buffer[:consumed]
            event = self._line(line)
            if event is not None:
                events.append(event)

    def finish(self) -> list[dict[str, Any]]:
        """Dispatch an event left without a closing blank line when the stream ends."""
        events: list[dict[str, Any]] = []
        if self._buffer:
            event = self._line(bytes(self._buffer))
            self._buffer.clear()
            if event is not None:
                events.append(event)
        event = self._dispatch()
        if event is not None:
            events.append(event)
        return events

    def _line_end(self) -> tuple[bytes, int] | None:
        lf = self._buffer.find(b"\n")
        cr = self._buffer.find(b"\r")
        if cr != -1 and (lf == -1 or cr < lf):
            if cr + 1 == len(self._buffer):
                return None  # wait: this may be the first half of \r\n
            return bytes(self._buffer[:cr]), cr + (2 if self._buffer[cr + 1] == 0x0A else 1)
        if lf != -1:
            return bytes(self._buffer[:lf]), lf + 1
        return None

    def _line(self, line: bytes) -> dict[str, Any] | None:
        if line == b"":
            return self._dispatch()
        if self._oversized or line.startswith(b":"):
            return None
        if line.startswith(b"data:"):
            value = line[5:].removeprefix(b" ")
            self._size += len(value) + 1
            if self._size > self._max:
                self._oversized = True
                self._data.clear()
            else:
                self._data.append(value)
        return None

    def _dispatch(self) -> dict[str, Any] | None:
        oversized, data = self._oversized, self._data
        self._data, self._size, self._oversized = [], 0, False
        if oversized:
            self.dropped += 1
            return {"type": "devpilot.oversized_event"}
        if not data:
            return None
        try:
            event = json.loads(b"\n".join(data))
        except ValueError:
            self.dropped += 1
            return {"type": "devpilot.invalid_event"}
        # /global/event wraps each event as {"directory": ..., "payload": {...}}.
        if isinstance(event, dict) and isinstance(event.get("payload"), dict):
            event = event["payload"]
        if not isinstance(event, dict):
            self.dropped += 1
            return {"type": "devpilot.invalid_event"}
        return event


def opencode_config_problem(settings: Settings, egress: EgressPolicy) -> str | None:
    """Why the OpenCode adapter cannot be used, or None. Never includes secret values."""
    if not settings.opencode_model:
        return "OPENCODE_MODEL is not set in .env"
    provider, _, model_id = settings.opencode_model.partition("/")
    if provider != "openai" or not model_id:
        return 'OPENCODE_MODEL must look like "openai/<model-id>": DevPilot uses only OpenAI models'
    parts = urlsplit(settings.opencode_base_url)
    if parts.scheme != "http" or not parts.hostname or parts.port is None:
        return "OPENCODE_BASE_URL must look like http://127.0.0.1:<port>"
    if parts.hostname != "localhost":
        try:
            loopback = ipaddress.ip_address(parts.hostname).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            return "OPENCODE_BASE_URL must be a loopback address: workspace contents never leave this machine"
    decision = egress.check("backend", settings.opencode_base_url)
    if not decision.allowed:
        return f"OPENCODE_BASE_URL is refused by the egress allowlist: {decision.reason}"
    if settings.opencode_server_password is None:
        return "OPENCODE_SERVER_PASSWORD is not set: the OpenCode server must require a password"
    return None


def _dotted(version: tuple[int, int, int]) -> str:
    return ".".join(map(str, version))
