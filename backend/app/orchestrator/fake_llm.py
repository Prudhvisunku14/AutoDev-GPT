"""FakeLLM: replays a scripted sequence of GPT responses (CLAUDE.md, Testing).

Used by the automated tests and the offline demo. It makes no network calls and
records every request so tests can assert exactly what GPT would have received.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.orchestrator.llm_client import FunctionCall, LLMError, LLMErrorKind, LLMRequest, LLMResponse, Usage

Step = Callable[[LLMRequest, int], LLMResponse]


@dataclass(frozen=True)
class _Call:
    name: str
    arguments: str
    call_id: str | None


def call(name: str, arguments: dict[str, Any] | str, *, call_id: str | None = None) -> Step:
    """A response containing one function call."""
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return calls(_Call(name, raw, call_id))


def calls(*items: _Call) -> Step:
    def step(_request: LLMRequest, n: int) -> LLMResponse:
        return LLMResponse(
            id=f"resp_fake_{n}",
            function_calls=[
                FunctionCall(call_id=c.call_id or f"call_fake_{n}_{i}", name=c.name, arguments=c.arguments)
                for i, c in enumerate(items)
            ],
            usage=Usage(input_tokens=1_000, output_tokens=200),
        )

    return step


def fcall(name: str, arguments: dict[str, Any] | str, *, call_id: str | None = None) -> _Call:
    """One item for `calls(...)` when a response holds several function calls."""
    return _Call(name, arguments if isinstance(arguments, str) else json.dumps(arguments), call_id)


def text(message: str) -> Step:
    """A response with no function call (GPT just talked)."""

    def step(_request: LLMRequest, n: int) -> LLMResponse:
        return LLMResponse(id=f"resp_fake_{n}", text=message, usage=Usage(input_tokens=500, output_tokens=50))

    return step


def fail(kind: LLMErrorKind, message: str = "scripted failure") -> Step:
    def step(_request: LLMRequest, _n: int) -> LLMResponse:
        raise LLMError(kind, message)

    return step


class FakeLLM:
    def __init__(self, *steps: Step) -> None:
        self._steps = list(steps)
        self._lock = threading.Lock()
        self.requests: list[LLMRequest] = []

    def extend(self, *steps: Step) -> None:
        with self._lock:
            self._steps.extend(steps)

    @property
    def remaining(self) -> int:
        return len(self._steps)

    def create(self, request: LLMRequest) -> LLMResponse:
        with self._lock:
            self.requests.append(request)
            n = len(self.requests)
            if not self._steps:
                raise LLMError(LLMErrorKind.UNEXPECTED, "FakeLLM script exhausted")
            step = self._steps.pop(0)
        return step(request, n)
