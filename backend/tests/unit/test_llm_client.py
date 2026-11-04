"""OpenAIResponsesClient against the real SDK with a mocked HTTP transport (no network, no cost)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.core.egress import EgressPolicy
from app.orchestrator.llm_client import (
    LLMError,
    LLMErrorKind,
    LLMRequest,
    OpenAIResponsesClient,
    llm_config_problem,
)
from tests.helpers import ALLOWLIST_FILE, FAKE_OPENAI_KEY, make_settings

MODEL = "test-model-from-settings"

RESPONSE_BODY: dict[str, Any] = {
    "id": "resp_123",
    "object": "response",
    "created_at": 0,
    "status": "completed",
    "model": MODEL,
    "output": [
        {"type": "reasoning", "id": "rs_1", "summary": []},
        {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_abc",
            "name": "submit_analysis",
            "arguments": '{"summary": "x"}',
            "status": "completed",
        },
        {
            "type": "message",
            "id": "msg_1",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "Submitting.", "annotations": []}],
        },
    ],
    "usage": {
        "input_tokens": 120,
        "output_tokens": 30,
        "total_tokens": 150,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens_details": {"reasoning_tokens": 10},
    },
    "parallel_tool_calls": False,
    "tool_choice": "required",
    "tools": [],
}

REQUEST = LLMRequest(
    instructions="Current state: ANALYZING.",
    input=[{"role": "user", "content": "hello"}],
    tools=[{"type": "function", "name": "submit_analysis", "description": "d", "parameters": {"type": "object"}, "strict": True}],
    previous_response_id="resp_prev",
)


def _client(tmp_path: Path, handler: Callable[[httpx.Request], httpx.Response], **overrides: Any) -> OpenAIResponsesClient:
    settings = make_settings(tmp_path, **{"openai_model": MODEL, "openai_max_retries": 0, "openai_max_output_tokens": 1_000, **overrides})
    return OpenAIResponsesClient(settings, http_client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda _seconds: None)  # a wait is never really waited here


def _error(status: int, code: str | None = None, message: str = "boom") -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": message, "type": code or "error", "code": code}})

    return handler


def test_request_shape_and_response_parsing(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=RESPONSE_BODY)

    response = _client(tmp_path, handler, openai_max_output_tokens=2048).create(REQUEST)

    (request,) = seen
    assert request.url.path.endswith("/responses")
    assert request.url.host == "api.openai.com"
    assert request.headers["Authorization"] == f"Bearer {FAKE_OPENAI_KEY}"
    body = json.loads(request.content)
    assert body["model"] == MODEL  # from settings, never hardcoded
    assert body["instructions"] == REQUEST.instructions
    assert body["input"] == REQUEST.input
    assert body["tools"] == REQUEST.tools
    assert body["previous_response_id"] == "resp_prev"
    assert body["parallel_tool_calls"] is False
    assert body["service_tier"] == "default", "auto follows the project's setting, which may be the dearer Fast tier"
    assert body["store"] is True
    assert body["tool_choice"] == "required"
    assert body["max_output_tokens"] == 2048

    assert response.id == "resp_123"
    assert [(c.call_id, c.name, c.arguments) for c in response.function_calls] == [
        ("call_abc", "submit_analysis", '{"summary": "x"}')
    ]
    assert response.text == "Submitting."
    assert (response.usage.input_tokens, response.usage.output_tokens) == (120, 30)
    assert response.status == "completed"


def test_first_turn_omits_previous_response_id_and_tool_choice_without_tools(tmp_path: Path) -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=RESPONSE_BODY)

    _client(tmp_path, handler).create(LLMRequest(instructions="i", input=[], tools=[], previous_response_id=None))
    assert "previous_response_id" not in seen[0]
    assert "tool_choice" not in seen[0]
    assert seen[0]["max_output_tokens"] == 1_000  # every call carries the cap the budget reserved


def test_incomplete_response_is_reported(tmp_path: Path) -> None:
    body = {**RESPONSE_BODY, "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}, "output": []}
    response = _client(tmp_path, lambda r: httpx.Response(200, json=body)).create(REQUEST)
    assert response.status == "incomplete"
    assert response.incomplete_reason == "max_output_tokens"
    assert response.function_calls == []


@pytest.mark.parametrize(
    ("handler", "kind", "resumable"),
    [
        (_error(401, "invalid_api_key"), LLMErrorKind.AUTH, True),
        (_error(403), LLMErrorKind.AUTH, True),
        (_error(429, "insufficient_quota"), LLMErrorKind.QUOTA, True),
        (_error(429, "rate_limit_exceeded"), LLMErrorKind.RATE_LIMIT, True),
        (_error(404, "model_not_found"), LLMErrorKind.MODEL_NOT_FOUND, True),
        (_error(400, "invalid_request_error"), LLMErrorKind.BAD_REQUEST, False),
        (_error(422), LLMErrorKind.BAD_REQUEST, False),
        (_error(500), LLMErrorKind.TRANSIENT, True),
        (_error(503), LLMErrorKind.TRANSIENT, True),
        (_error(418), LLMErrorKind.UNEXPECTED, False),
    ],
)
def test_http_errors_map_to_structured_kinds(
    tmp_path: Path, handler: Callable[[httpx.Request], httpx.Response], kind: LLMErrorKind, resumable: bool
) -> None:
    with pytest.raises(LLMError) as info:
        _client(tmp_path, handler).create(REQUEST)
    assert info.value.kind is kind
    assert info.value.resumable is resumable
    assert FAKE_OPENAI_KEY not in info.value.message


def test_timeout_maps_to_timeout(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(LLMError) as info:
        _client(tmp_path, handler).create(REQUEST)
    assert info.value.kind is LLMErrorKind.TIMEOUT


def test_connection_error_maps_to_transient(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(LLMError) as info:
        _client(tmp_path, handler).create(REQUEST)
    assert info.value.kind is LLMErrorKind.TRANSIENT


def test_a_transient_server_error_is_sent_once_and_never_retried_by_the_sdk(tmp_path: Path) -> None:
    """A 500 may come after the call was processed and billed; resending it would be counted once but billed twice."""
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(500, json={"error": {"message": "flaky"}}, headers={"retry-after-ms": "1"})

    with pytest.raises(LLMError):
        _client(tmp_path, handler).create(REQUEST)
    assert len(attempts) == 1


@pytest.mark.parametrize(("overrides", "problem"), [({"openai_max_output_tokens": None}, "OPENAI_MAX_OUTPUT_TOKENS is not set"), ({"openai_max_retries": 2}, "OPENAI_MAX_RETRIES=2")])
def test_a_client_whose_cost_cannot_be_bounded_is_never_built(tmp_path: Path, overrides: dict[str, Any], problem: str) -> None:
    sent: list[httpx.Request] = []
    with pytest.raises(LLMError) as info:
        _client(tmp_path, lambda r: (sent.append(r), httpx.Response(200, json=RESPONSE_BODY))[1], **overrides)
    assert info.value.kind is LLMErrorKind.CONFIG and problem in str(info.value) and sent == []
    full = {"openai_model": MODEL, "openai_max_retries": 0, "openai_max_output_tokens": 1_000, **overrides}
    assert problem in (llm_config_problem(make_settings(tmp_path, **full), EgressPolicy.from_file(ALLOWLIST_FILE)) or "")


def test_client_requires_key_and_model(tmp_path: Path) -> None:
    with pytest.raises(LLMError) as info:
        OpenAIResponsesClient(make_settings(tmp_path, openai_api_key="", openai_model=MODEL))
    assert info.value.kind is LLMErrorKind.CONFIG
    with pytest.raises(LLMError):
        OpenAIResponsesClient(make_settings(tmp_path, openai_model=None))


@pytest.mark.parametrize(
    ("overrides", "problem"),
    [
        ({"openai_api_key": ""}, "OPENAI_API_KEY is not set"),
        ({"openai_model": ""}, "OPENAI_MODEL is not set"),
        ({"openai_model": MODEL, "openai_store_responses": False}, "OPENAI_STORE_RESPONSES=false"),
        ({"openai_model": MODEL, "openai_base_url": "https://evil.example.com/v1"}, "refused by the egress allowlist"),
        ({"openai_model": MODEL, "openai_base_url": "http://api.openai.com/v1"}, "refused by the egress allowlist"),
    ],
)
def test_config_problems_are_reported_without_secrets(tmp_path: Path, overrides: dict[str, Any], problem: str) -> None:
    message = llm_config_problem(make_settings(tmp_path, **overrides), EgressPolicy.from_file(ALLOWLIST_FILE))
    assert message is not None and problem in message
    assert FAKE_OPENAI_KEY not in message


def test_complete_config_has_no_problem(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, openai_model=MODEL, openai_base_url="https://api.openai.com/v1", openai_max_output_tokens=4_000, openai_max_retries=0)
    assert llm_config_problem(settings, EgressPolicy.from_file(ALLOWLIST_FILE)) is None
