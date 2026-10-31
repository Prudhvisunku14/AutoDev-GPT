"""The read-only check cannot change the server: both layers refuse, and what they refuse never leaves.

No server and no HTTP answers here. These tests prove only the refusals and the schema comparison;
scripts/opencode_readonly_http.py does the reading on the real server, once, with approval.

Held back with the adapter: the script imports it.
"""

from __future__ import annotations

import importlib.util
import inspect
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def harness() -> ModuleType:
    return _load("devpilot_readonly_http", SCRIPTS / "opencode_readonly_http.py")


@pytest.fixture(scope="module")
def relay() -> ModuleType:
    return _load("devpilot_http_relay", SCRIPTS / "opencode_check" / "http_relay.py")


def _never(payload: bytes) -> dict[str, Any]:
    raise AssertionError("a refused request reached the relay")


MUTATING = ["POST", "PATCH", "PUT", "DELETE", "OPTIONS", "HEAD", "TRACE", "CONNECT"]
PATHS = ["/session", "/session/ses_1/prompt_async", "/session/ses_1/abort", "/session/ses_1/shell", "/config", "/auth/openai", "/pty"]


@pytest.mark.parametrize("method", MUTATING)
@pytest.mark.parametrize("path", PATHS)
def test_the_host_refuses_every_other_method_before_anything_leaves(harness: ModuleType, method: str, path: str) -> None:
    transport = harness.RelayTransport(_never)
    with pytest.raises(harness.ReadOnlyViolation, match="read-only"):
        transport.handle_request(httpx.Request(method, f"http://x{path}"))
    assert transport.forwarded == [] and transport.refused == [f"{method} {path}"]


@pytest.mark.parametrize("path", ["/pty", "/pty/x/connect", "/mcp", "/tui/show-toast", "/provider", "/auth", "/session/ses_1/shell", "/session/ses_1/diff",
                                  "/session/ses_1/message/msg_1", "/session/msg_1", "/experimental/worktree", "/api/session"])  # fmt: skip
def test_the_host_refuses_a_get_outside_its_allowlist(harness: ModuleType, path: str) -> None:
    transport = harness.RelayTransport(_never)
    with pytest.raises(harness.ReadOnlyViolation, match="allowlist"):
        transport.handle_request(httpx.Request("GET", f"http://x{path}"))


def test_the_clients_writing_calls_are_refused_by_the_transport(harness: ModuleType) -> None:
    transport = harness.RelayTransport(_never)
    client = harness.OpenCodeClient("http://x", username="u", password="p", directory="/workspace", transport=transport)
    for call in (lambda: client.create_session("t"), lambda: client.abort("ses_1"), lambda: client.prompt_async("ses_1", {"parts": []})):
        with pytest.raises(harness.ReadOnlyViolation):
            call()
    assert transport.forwarded == [] and len(transport.refused) == 3


@pytest.mark.parametrize("method", MUTATING)
def test_the_relay_refuses_every_other_method_on_its_own(relay: ModuleType, method: str) -> None:
    assert "only GET" in relay.refusal({"method": method, "path": "/session"})


@pytest.mark.parametrize("path", ["/pty", "/mcp", "/provider", "/session/ses_1/shell", "/session/ses_1/prompt_async", "/session/ses_1/abort",
                                  "/session/../config", "/session/ses_1/../../pty", "/x", "", None, 7])  # fmt: skip
def test_the_relay_refuses_a_path_outside_its_allowlist(relay: ModuleType, path: Any) -> None:
    assert "allowlist" in relay.refusal({"method": "GET", "path": path})


@pytest.mark.parametrize("query", ["directory=%2Fetc", "directory=/", "a=b", "directory=%2Fworkspace&x=1", 7])
def test_the_relay_refuses_any_other_query(relay: ModuleType, query: Any) -> None:
    assert "query" in relay.refusal({"method": "GET", "path": "/config", "query": query})


@pytest.mark.parametrize("request_value", [None, [], "GET /config", 7])
def test_the_relay_refuses_what_is_not_a_request(relay: ModuleType, request_value: Any) -> None:
    assert relay.refusal(request_value) == "the request is not an object"


def test_the_relay_allows_only_what_the_host_does(harness: ModuleType, relay: ModuleType) -> None:
    assert relay.ALLOWED == harness.READ_ONLY_PATHS
    for path in sorted(relay.ALLOWED):
        assert relay.refusal({"method": "GET", "path": path, "query": ""}) is None
        assert harness.read_only_refusal("GET", path) is None


def test_the_relay_holds_no_way_to_send_anything_but_get(relay: ModuleType) -> None:
    source = inspect.getsource(relay)
    assert re.findall(r'\.request\("([A-Z]+)"', source) == ["GET"]
    assert 'request["method"]' not in source  # the method is never taken from the request
    assert not re.search(r"\bsubprocess\b|\bos\.system\b|\beval\(|\bexec\(", source)


def test_the_password_is_not_on_a_command_line(harness: ModuleType) -> None:
    source = inspect.getsource(harness.exercise)
    assert "input=payload" in source and '"-i"' in source
    args = re.search(r"args = \[(.*?)\]", source)
    assert args is not None and "password" not in args.group(1)


# --- Comparing a real answer with the server's own schema -----------------------------------------


SCHEMAS = {"Thing": {"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}, "n": {"type": "integer"}}}}


def test_a_null_in_an_optional_field_is_noted_by_path_and_a_required_null_is_a_problem(harness: ModuleType) -> None:
    # The real server sends null for optional fields it declares as boolean, number or string.
    nulls: list[str] = []
    schema = {"type": "array", "items": {"$ref": "#/components/schemas/Thing"}}
    assert harness.schema_problems([{"id": "a", "n": None}], schema, SCHEMAS, nulls=nulls) == []
    assert nulls == ["$[0].n"]
    assert any("expected string" in p for p in harness.schema_problems([{"id": None}], schema, SCHEMAS, nulls=[]))
    assert any("expected integer" in p for p in harness.schema_problems([{"id": "a", "n": None}], schema, SCHEMAS))


@pytest.mark.parametrize(
    ("value", "schema"),
    [
        ({"id": "a", "n": 1}, {"$ref": "#/components/schemas/Thing"}),
        ([{"id": "a"}], {"type": "array", "items": {"$ref": "#/components/schemas/Thing"}}),
        ("x", {"anyOf": [{"type": "integer"}, {"type": "string"}]}),
        (None, {"type": ["string", "null"]}),
        ("x", {"type": ["string", "null"]}),
        (True, {"const": True}),
        ("a", {"enum": ["a", "b"]}),
        ({"x": {"id": "a"}}, {"type": "object", "additionalProperties": {"$ref": "#/components/schemas/Thing"}}),
        (1.5, {"type": "number"}),
    ],
)
def test_a_matching_value_has_no_problems(harness: ModuleType, value: Any, schema: Any) -> None:
    assert harness.schema_problems(value, schema, SCHEMAS) == []


@pytest.mark.parametrize(
    ("value", "schema", "reason"),
    [
        ({"n": 1}, {"$ref": "#/components/schemas/Thing"}, "required, missing"),
        ({"id": 1}, {"$ref": "#/components/schemas/Thing"}, "expected string"),
        ([{"id": 1}], {"type": "array", "items": {"$ref": "#/components/schemas/Thing"}}, "$[0].id"),
        (True, {"anyOf": [{"type": "integer"}, {"type": "string"}]}, "none of its 2 forms"),
        (7, {"type": ["string", "null"]}, "expected string or null"),
        (False, {"const": True}, "expected True"),
        ("c", {"enum": ["a", "b"]}, "not one of"),
        ({"x": {}}, {"type": "object", "additionalProperties": {"$ref": "#/components/schemas/Thing"}}, "$.x.id"),
        (True, {"type": "integer"}, "expected integer"),
        (True, {"type": "number"}, "expected number"),
        ("<html>", {"type": "object"}, "expected object"),
    ],
)
def test_a_value_that_leaves_the_schema_is_named(harness: ModuleType, value: Any, schema: Any, reason: str) -> None:
    assert any(reason in p for p in harness.schema_problems(value, schema, SCHEMAS))
