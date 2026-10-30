"""Pure contract checks for OpenCode: versions, the /doc document, SSE parsing, paths, tools, config.

No server and no HTTP: every input is a plain value.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from app.coding.opencode_contract import (
    ALLOWED_TOOLS,
    REQUIRED_OPERATIONS,
    TOOL_PERMISSIONS,
    ContractError,
    SSEParser,
    check_health,
    check_spec,
    inside_workspace,
    opencode_config_problem,
    operations,
    parse_version,
    sensitive_operations,
    spec_from_doc,
    template,
    tool_violation,
)
from app.core.egress import EgressPolicy
from tests.helpers import make_settings

# --- Versions -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "parsed"),
    [("1.19.5", (1, 19, 5)), ("v1.19.5", (1, 19, 5)), (" 1.19.5-beta.2 ", (1, 19, 5)), ("1.19", None), ("abc", None), ("", None)],
)
def test_version_parsing(text: str, parsed: tuple[int, int, int] | None) -> None:
    assert parse_version(text) == parsed


@pytest.mark.parametrize("version", ["1.19.0", "1.19.5", "1.99.0"])
def test_supported_versions_pass(version: str) -> None:
    assert check_health({"healthy": True, "version": version}) == version


@pytest.mark.parametrize("version", ["1.18.9", "2.0.0", "2.2.2", "garbage", ""])
def test_unsupported_versions_are_incompatible(version: str) -> None:
    with pytest.raises(ContractError) as info:
        check_health({"healthy": True, "version": version})
    assert info.value.kind == "incompatible"


@pytest.mark.parametrize("payload", [{"healthy": False, "version": "1.19.5"}, {"version": "1.19.5"}, "ok", None])
def test_an_unhealthy_or_malformed_health_answer_is_unavailable(payload: Any) -> None:
    with pytest.raises(ContractError) as info:
        check_health(payload)
    assert info.value.kind == "unavailable"


# --- The /doc document ----------------------------------------------------------------


def _spec(ops: set[tuple[str, str]]) -> dict[str, Any]:
    paths: dict[str, dict[str, Any]] = {}
    for method, path in ops:
        concrete = path.replace("{}", "{id}")
        paths.setdefault(concrete, {})[method] = {}
    return {"openapi": "3.1.0", "paths": paths}


FULL = set(REQUIRED_OPERATIONS) | {("post", "/permission/{}/reply"), ("post", "/question/{}/reject")}


def test_an_html_doc_page_is_refused_not_scraped() -> None:
    page = b"<!doctype html><html><body><script>spec = {}</script></body></html>"
    with pytest.raises(ContractError) as info:
        spec_from_doc("text/html; charset=utf-8", page)
    assert info.value.kind == "incompatible" and "HTML page" in info.value.message


def test_html_is_recognised_even_with_a_misleading_content_type() -> None:
    with pytest.raises(ContractError, match="HTML page"):
        spec_from_doc("application/json", b"  <html></html>")


def test_a_json_openapi_document_is_accepted() -> None:
    spec = _spec(FULL)
    assert spec_from_doc("application/json", json.dumps(spec).encode()) == spec


@pytest.mark.parametrize(
    ("body", "kind"),
    [(b"not json", "protocol"), (b"[1, 2]", "incompatible"), (b'{"openapi": "3.1.0"}', "incompatible")],
)
def test_other_doc_answers_are_refused(body: bytes, kind: str) -> None:
    with pytest.raises(ContractError) as info:
        spec_from_doc("application/json", body)
    assert info.value.kind == kind


def test_operations_are_normalized_from_any_parameter_names() -> None:
    spec = {"paths": {"/session/{sessionID}/abort": {"post": {}, "parameters": []}, "/x/": {"GET": {}}, 5: {"get": {}}}}
    assert operations(spec) == {("post", "/session/{}/abort"), ("get", "/x")}
    assert template("/permission/{requestID}/reply") == "/permission/{}/reply"
    assert template("/") == "/"


def test_a_complete_spec_gives_a_contract_using_the_newer_reply_operation() -> None:
    contract = check_spec(_spec(FULL), "1.19.5")
    assert contract.permission_reply == "/permission/{}/reply"
    assert contract.question_reject == "/question/{}/reject"


def test_the_older_reply_operation_is_used_when_it_is_the_only_one() -> None:
    ops = set(REQUIRED_OPERATIONS) | {("post", "/session/{}/permissions/{}")}
    contract = check_spec(_spec(ops), "1.19.5")
    assert contract.permission_reply == "/session/{}/permissions/{}"
    assert contract.question_reject is None


def test_a_missing_operation_is_named() -> None:
    ops = FULL - {("post", "/session/{}/prompt_async")}
    with pytest.raises(ContractError, match=r"POST /session/\{\}/prompt_async"):
        check_spec(_spec(ops), "1.19.5")


def test_no_way_to_answer_permissions_is_incompatible() -> None:
    with pytest.raises(ContractError, match="answer permission requests"):
        check_spec(_spec(set(REQUIRED_OPERATIONS)), "1.19.5")


def test_sensitive_operations_are_inventoried() -> None:
    ops = FULL | {("post", "/session/{}/shell"), ("patch", "/config"), ("put", "/auth/{}"), ("get", "/mcp"), ("post", "/tui/x")}
    contract = check_spec(_spec(ops), "1.19.5")
    assert contract.sensitive == ("GET /mcp", "PATCH /config", "POST /session/{}/shell", "POST /tui/x", "PUT /auth/{}")
    assert sensitive_operations(set(FULL)) == []


# --- SSE parsing --------------------------------------------------------------------------


def _frames(*objects: Any) -> bytes:
    return b"".join(b"data: " + json.dumps(o).encode() + b"\n\n" for o in objects)


def test_events_are_parsed_across_arbitrary_chunk_boundaries() -> None:
    raw = _frames({"type": "a"}, {"type": "b", "properties": {"n": 1}})
    parser = SSEParser()
    events = [e for i in range(len(raw)) for e in parser.feed(raw[i : i + 1])]
    assert events == [{"type": "a"}, {"type": "b", "properties": {"n": 1}}]


def test_crlf_and_cr_line_endings_are_accepted() -> None:
    parser = SSEParser()
    assert parser.feed(b'data: {"type": "a"}\r\n\r\ndata: {"type": "b"}\r\r\n') == [{"type": "a"}, {"type": "b"}]


def test_a_cr_at_the_end_of_a_chunk_waits_for_the_next_byte() -> None:
    parser = SSEParser()
    assert parser.feed(b'data: {"type": "a"}\r') == []
    assert parser.feed(b"\n\r\n") == [{"type": "a"}]


def test_comments_other_fields_and_multi_line_data_are_handled() -> None:
    raw = b': keepalive\nevent: message\nid: 7\ndata: {"type":\ndata: "joined"}\n\n'
    assert SSEParser().feed(raw) == [{"type": "joined"}]


def test_a_wrapped_global_event_is_unwrapped() -> None:
    assert SSEParser().feed(_frames({"directory": "/workspace", "payload": {"type": "a"}})) == [{"type": "a"}]


@pytest.mark.parametrize("data", [b"not json", b"[1, 2]", b'"text"'])
def test_invalid_events_are_reported_not_raised(data: bytes) -> None:
    parser = SSEParser()
    assert parser.feed(b"data: " + data + b"\n\n") == [{"type": "devpilot.invalid_event"}]
    assert parser.dropped == 1


def test_an_oversized_event_is_dropped_and_parsing_continues() -> None:
    parser = SSEParser(max_event_bytes=64)
    big = b"data: " + b'{"x": "' + b"y" * 200 + b'"}\n\n'
    assert parser.feed(big + _frames({"type": "after"})) == [{"type": "devpilot.oversized_event"}, {"type": "after"}]


def test_an_endless_line_is_not_buffered_beyond_the_cap() -> None:
    parser = SSEParser(max_event_bytes=64)
    for _ in range(100):
        assert parser.feed(b"x" * 50) == []
    assert len(parser._buffer) <= 64  # noqa: SLF001 - checking the memory bound itself
    assert parser.feed(b"\n\n") == [{"type": "devpilot.oversized_event"}]


def test_many_small_data_lines_still_hit_the_cap() -> None:
    parser = SSEParser(max_event_bytes=64)
    events = parser.feed(b"data: 1\n" * 40 + b"\n")
    assert events == [{"type": "devpilot.oversized_event"}]


def test_an_event_without_a_final_blank_line_is_dispatched_at_the_end() -> None:
    parser = SSEParser()
    assert parser.feed(b'data: {"type": "last"}') == []
    assert parser.finish() == [{"type": "last"}]


# --- Paths and tools ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "inside"),
    [
        ("/workspace/a.py", True),
        ("/workspace/sub/./b.py", True),
        ("//workspace//c.py", True),
        ("/workspace/sub/../d.py", True),
        ("/workspace", False),
        ("/workspace/", False),
        ("/workspace/../etc/passwd", False),
        ("/workspace-other/a.py", False),
        ("/etc/passwd", False),
        ("relative/a.py", False),
        ("/../workspace/a.py", False),
    ],
)
def test_workspace_containment(path: str, inside: bool) -> None:
    assert inside_workspace(path, "/workspace") is inside


def test_only_allowlisted_tools_may_run() -> None:
    for tool in ("write", "edit", "apply_patch", "read", "glob", "grep", "todowrite", "invalid"):
        assert tool_violation(tool, "completed") is None
    assert tool_violation("bash", "pending") is None  # requested, not run: the permission system decides
    # bash and friends, plus tools 1.19.5 does not have (a newer release adding one is refused until reviewed)
    for tool in ("bash", "webfetch", "task", "question", "interactive_shell", "multiedit", "patch", "list", "some_future_tool"):
        assert "does not allow" in (tool_violation(tool, "running") or "")
        assert "does not allow" in (tool_violation(tool, "completed") or "")
    assert {"bash", "webfetch", "websearch", "task", "skill", "question", "lsp", "codesearch"}.isdisjoint(ALLOWED_TOOLS)


def test_every_allowed_tool_maps_to_a_reading_or_editing_permission() -> None:
    # From the 1.19.5 binary: edit, write and apply_patch ask "edit"; invalid asks nothing.
    assert {TOOL_PERMISSIONS[t] for t in ALLOWED_TOOLS} == {"read", "glob", "grep", "edit", "todowrite", None}
    assert [t for t in ("edit", "write", "apply_patch") if TOOL_PERMISSIONS[t] != "edit"] == []
    assert ALLOWED_TOOLS <= TOOL_PERMISSIONS.keys()
    assert all(TOOL_PERMISSIONS[t] == t for t in TOOL_PERMISSIONS.keys() - ALLOWED_TOOLS)


# --- Configuration ------------------------------------------------------------------------


@pytest.fixture
def egress() -> EgressPolicy:
    return EgressPolicy({"backend": ["api.openai.com", "127.0.0.1:4096"]})


def _settings(tmp_path: Path, **overrides: object):  # noqa: ANN202
    values: dict[str, object] = {"opencode_model": "openai/test-model", "opencode_server_password": SecretStr("p" * 32)}
    values.update(overrides)
    return make_settings(tmp_path, **values)


def test_a_complete_local_configuration_has_no_problem(tmp_path: Path, egress: EgressPolicy) -> None:
    assert opencode_config_problem(_settings(tmp_path), egress) is None


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"opencode_model": None}, "OPENCODE_MODEL is not set"),
        ({"opencode_model": "anthropic/x"}, "only OpenAI models"),
        ({"opencode_model": "openai/"}, "only OpenAI models"),
        ({"opencode_base_url": "https://127.0.0.1:4096"}, "must look like"),
        ({"opencode_base_url": "http://127.0.0.1"}, "must look like"),
        ({"opencode_base_url": "http://192.168.1.10:4096"}, "loopback"),
        ({"opencode_base_url": "http://example.com:4096"}, "loopback"),
        ({"opencode_base_url": "http://127.0.0.1:5000"}, "egress allowlist"),
        ({"opencode_server_password": None}, "OPENCODE_SERVER_PASSWORD is not set"),
    ],
)
def test_configuration_problems_are_named(tmp_path: Path, egress: EgressPolicy, overrides: dict[str, object], expected: str) -> None:
    problem = opencode_config_problem(_settings(tmp_path, **overrides), egress)
    assert problem is not None and expected in problem


def test_a_problem_message_never_contains_the_password(tmp_path: Path) -> None:
    secret = "s" * 40
    settings = _settings(tmp_path, opencode_server_password=SecretStr(secret))
    problem = opencode_config_problem(settings, EgressPolicy({"backend": []}))
    assert problem is not None and secret not in problem
