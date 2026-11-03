"""LLMClient: the only path to GPT (OpenAI Responses API with function calling).

`OpenAIResponsesClient` wraps the official SDK. Retries with backoff for connection
errors, 408, 409, 429 and 5xx are done by the SDK (`OPENAI_MAX_RETRIES`). Whatever
still fails is mapped to an `LLMError` with a stable `kind` the orchestrator acts on.
Model IDs, URLs and keys come only from Settings.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

import httpx
import openai

from app.config import Settings
from app.core.egress import EgressPolicy

DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"


class LLMErrorKind(StrEnum):
    AUTH = "AUTH"  # 401/403: bad or missing key, no access to the model
    QUOTA = "QUOTA"  # 429 insufficient_quota: billing or quota exhausted
    RATE_LIMIT = "RATE_LIMIT"  # 429 after retries
    MODEL_NOT_FOUND = "MODEL_NOT_FOUND"  # 404: OPENAI_MODEL unknown to this key
    TIMEOUT = "TIMEOUT"
    TRANSIENT = "TRANSIENT"  # connection errors and 5xx after retries
    BAD_REQUEST = "BAD_REQUEST"  # 400/422: the request itself is wrong
    CONFIG = "CONFIG"  # local configuration is missing or refused
    UNEXPECTED = "UNEXPECTED"
    # Not a fault: the caller refused to send the request (the smoke-test guard, a dry run).
    # The run pauses as "llm_stopped" and no llm.error event is recorded.
    STOPPED = "STOPPED"


# A run hitting one of these pauses and can be resumed after the cause is fixed.
RESUMABLE_ERROR_KINDS = frozenset(
    {
        LLMErrorKind.AUTH,
        LLMErrorKind.QUOTA,
        LLMErrorKind.RATE_LIMIT,
        LLMErrorKind.MODEL_NOT_FOUND,
        LLMErrorKind.TIMEOUT,
        LLMErrorKind.TRANSIENT,
        LLMErrorKind.CONFIG,
    }
)


class LLMError(Exception):
    def __init__(
        self, kind: LLMErrorKind, message: str, *, status_code: int | None = None, code: str | None = None, request_id: str | None = None,
        retry_after: float | None = None, rate_limit: dict[str, str] | None = None,
    ) -> None:  # fmt: skip
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status_code = status_code
        self.code = code  # OpenAI's error code, such as rate_limit_exceeded or insufficient_quota
        self.request_id = request_id  # what to quote to OpenAI's support
        self.retry_after = retry_after  # seconds, from Retry-After when OpenAI sent one
        self.rate_limit = rate_limit or {}  # the x-ratelimit-* headers. Never an authorization header

    def facts(self) -> dict[str, Any]:
        """What is known about the failure beyond its message, for the run's event log."""
        found = {"status_code": self.status_code, "code": self.code, "request_id": self.request_id, "retry_after": self.retry_after, "rate_limit": self.rate_limit or None}
        return {k: v for k, v in found.items() if v is not None}

    @property
    def resumable(self) -> bool:
        return self.kind in RESUMABLE_ERROR_KINDS


@dataclass(frozen=True)
class FunctionCall:
    call_id: str
    name: str
    arguments: str  # raw JSON text exactly as GPT produced it


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class LLMRequest:
    instructions: str
    input: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    previous_response_id: str | None


@dataclass(frozen=True)
class LLMResponse:
    id: str
    function_calls: list[FunctionCall] = field(default_factory=list)
    text: str = ""
    usage: Usage = Usage()
    status: str = "completed"
    incomplete_reason: str | None = None


class LLMClient(Protocol):
    def create(self, request: LLMRequest) -> LLMResponse: ...


def llm_config_problem(settings: Settings, egress: EgressPolicy) -> str | None:
    """Why the real client cannot be used, or None. Never includes secret values."""
    if settings.openai_api_key is None:
        return "OPENAI_API_KEY is not set in .env"
    if not settings.openai_model:
        return "OPENAI_MODEL is not set in .env"
    if not settings.openai_store_responses:
        return "OPENAI_STORE_RESPONSES=false (transcript replay) is not supported yet; set it to true"
    base_url = settings.openai_base_url or DEFAULT_OPENAI_BASE_URL
    decision = egress.check("backend", base_url)
    if not decision.allowed:
        return f"OPENAI_BASE_URL is refused by the egress allowlist: {decision.reason}"
    return None


# A 429 whose code says "rate_limit_exceeded" is temporary and was never processed, so it is not billed. It may be waited out, a
# couple of times and for a bounded while. Anything that says the money or the account is the problem is final (see map_openai_error).
RATE_RETRIES = 2
RATE_WAIT_CAP = 20.0  # seconds: a Retry-After beyond this is not waited for, and the run pauses instead
RATE_WAIT_TOTAL = 40.0
_DEFAULT_WAITS = (1.0, 3.0)
HARD_QUOTA_CODES = frozenset({"insufficient_quota", "billing_hard_limit_reached", "billing_not_active", "account_deactivated", "access_terminated"})
_RATE_HEADERS = ("x-ratelimit-limit-requests", "x-ratelimit-limit-tokens", "x-ratelimit-remaining-requests", "x-ratelimit-remaining-tokens", "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens")
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9 ._:,;=+/-]{1,64}$")


def rate_wait(error: LLMError, tries: int, waited: float) -> float | None:
    """How long to wait before sending the request again, or None when it must not be sent again."""
    if error.kind is not LLMErrorKind.RATE_LIMIT or error.code != "rate_limit_exceeded" or tries >= RATE_RETRIES:
        return None
    wait = _DEFAULT_WAITS[tries] if error.retry_after is None else error.retry_after
    return wait if wait <= RATE_WAIT_CAP and waited + wait <= RATE_WAIT_TOTAL else None


class OpenAIResponsesClient:
    def __init__(self, settings: Settings, *, http_client: httpx.Client | None = None, sleep: Callable[[float], None] = time.sleep) -> None:
        if settings.openai_api_key is None or not settings.openai_model:
            raise LLMError(LLMErrorKind.CONFIG, "OPENAI_API_KEY and OPENAI_MODEL must be set")
        self._sleep = sleep
        self._model = settings.openai_model
        self._max_output_tokens = settings.openai_max_output_tokens
        self._client = openai.OpenAI(
            api_key=settings.openai_api_key.get_secret_value(),
            base_url=settings.openai_base_url or DEFAULT_OPENAI_BASE_URL,
            timeout=settings.openai_timeout_seconds,
            max_retries=settings.openai_max_retries,
            http_client=http_client,
        )

    def create(self, request: LLMRequest) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "instructions": request.instructions,
            "input": request.input,
            "tools": request.tools,
            "parallel_tool_calls": False,
            "store": True,
            "service_tier": "default",  # "auto" follows the project's setting, which may be the dearer Fast tier
        }
        if request.tools:
            kwargs["tool_choice"] = "required"
        if request.previous_response_id:
            kwargs["previous_response_id"] = request.previous_response_id
        if self._max_output_tokens:
            kwargs["max_output_tokens"] = self._max_output_tokens
        tries, waited = 0, 0.0
        while True:
            try:
                response = self._client.responses.create(**kwargs)
                break
            except openai.OpenAIError as exc:
                error = map_openai_error(exc)
                wait = rate_wait(error, tries, waited)
                if wait is None:
                    raise error from None
                tries, waited = tries + 1, waited + wait
                self._sleep(wait)
        return _parse_response(response)


def _headers_of(exc: openai.APIStatusError) -> dict[str, str]:
    try:
        return {str(k).lower(): str(v).strip() for k, v in exc.response.headers.items()}
    except AttributeError:
        return {}


def _seconds(headers: dict[str, str]) -> float | None:
    """Retry-After, in seconds. Anything that is not a plain non-negative number (an HTTP date, say) is not used."""
    for name, scale in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
        try:
            value = float(headers[name]) * scale
        except (KeyError, ValueError):
            continue
        if 0 <= value < float("inf"):
            return value
    return None


def map_openai_error(exc: openai.OpenAIError) -> LLMError:
    # Messages come from the API and never contain the key; they are redacted again on storage.
    if isinstance(exc, openai.APITimeoutError):
        return LLMError(LLMErrorKind.TIMEOUT, "OpenAI request timed out")
    if isinstance(exc, openai.APIConnectionError):
        return LLMError(LLMErrorKind.TRANSIENT, "could not connect to the OpenAI API")
    if isinstance(exc, openai.APIStatusError):
        status = exc.status_code
        code = getattr(exc, "code", None)
        code = code if isinstance(code, str) else None
        detail = _short(exc.message)
        headers = _headers_of(exc)
        request_id = headers.get("x-request-id")
        facts: dict[str, Any] = {
            "status_code": status, "code": code, "request_id": request_id if request_id and _SAFE_VALUE.fullmatch(request_id) else None,
            "retry_after": _seconds(headers), "rate_limit": {k: headers[k] for k in _RATE_HEADERS if k in headers and _SAFE_VALUE.fullmatch(headers[k])},
        }  # fmt: skip
        if isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError):
            return LLMError(LLMErrorKind.AUTH, f"OpenAI rejected the credentials ({status}): {detail}", **facts)
        if isinstance(exc, openai.RateLimitError):
            kind = LLMErrorKind.QUOTA if code in HARD_QUOTA_CODES else LLMErrorKind.RATE_LIMIT
            return LLMError(kind, f"OpenAI rate limit or quota ({status}): {detail}", **facts)
        if isinstance(exc, openai.NotFoundError):
            return LLMError(LLMErrorKind.MODEL_NOT_FOUND, f"OpenAI resource not found ({status}): {detail}", status_code=status)
        if isinstance(exc, openai.BadRequestError | openai.UnprocessableEntityError):
            return LLMError(LLMErrorKind.BAD_REQUEST, f"OpenAI refused the request ({status}): {detail}", status_code=status)
        if status >= 500:
            return LLMError(LLMErrorKind.TRANSIENT, f"OpenAI server error ({status})", status_code=status)
        return LLMError(LLMErrorKind.UNEXPECTED, f"OpenAI error ({status}): {detail}", status_code=status)
    return LLMError(LLMErrorKind.UNEXPECTED, f"OpenAI client error: {type(exc).__name__}")


def _parse_response(response: Any) -> LLMResponse:
    calls: list[FunctionCall] = []
    texts: list[str] = []
    for item in getattr(response, "output", None) or []:
        item_type = getattr(item, "type", None)
        if item_type == "function_call":
            calls.append(FunctionCall(call_id=item.call_id, name=item.name, arguments=item.arguments or ""))
        elif item_type == "message":
            for part in getattr(item, "content", None) or []:
                text = getattr(part, "text", None)
                if isinstance(text, str):
                    texts.append(text)
    usage = getattr(response, "usage", None)
    incomplete = getattr(response, "incomplete_details", None)
    return LLMResponse(
        id=response.id,
        function_calls=calls,
        text="\n".join(texts),
        usage=Usage(
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        ),
        status=str(getattr(response, "status", "completed") or "completed"),
        incomplete_reason=getattr(incomplete, "reason", None),
    )


def _short(message: object, limit: int = 300) -> str:
    text = message if isinstance(message, str) else json.dumps(message, default=str)
    return text if len(text) <= limit else text[: limit - 3] + "..."
