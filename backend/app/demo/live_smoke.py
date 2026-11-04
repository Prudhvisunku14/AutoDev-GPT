"""Minimal live GPT smoke test: can the real model submit a structured analysis?

The requirement is a fixed, public sentence. The run goes through the real API,
orchestrator and gate validation. `SmokeGuardClient` sits between the orchestrator
and the OpenAI client and enforces the limits in code:

- at most MAX_CALLS requests to OpenAI (the SDK's own retries are disabled);
- only ANALYZING-state requests: once the analysis is accepted, the run stops instead of planning;
- a second call (a retry after a rejected analysis) only if the first used at most
  SECOND_CALL_MAX_TOKENS_USED tokens, so the total stays within TOKEN_LIMIT;
- each call's output is capped at MAX_OUTPUT_TOKENS (OPENAI_MAX_OUTPUT_TOKENS);
- a request is refused before sending if any configured secret appears in it.

Nothing is generated, executed, published or deployed.
"""

from __future__ import annotations

import dataclasses
import json
import secrets
import tempfile
import time
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.config import REPO_ROOT, Settings
from app.orchestrator.llm_client import LLMClient, LLMError, LLMErrorKind, LLMRequest, LLMResponse

REQUIREMENT = "Create a simple Python calculator application."
PROJECT_NAME = "Calculator smoke test"

MAX_CALLS = 2
TOKEN_LIMIT = 8_000
MAX_OUTPUT_TOKENS = 2_000
# With previous_response_id, call 2's input re-bills call 1 (T1) plus the validation errors (F),
# so the total is about 2*T1 + F + MAX_OUTPUT_TOKENS: 2*2,500 + ~500 + 2,000 = ~7,500.
SECOND_CALL_MAX_TOKENS_USED = 2_500
TIMEOUT_SECONDS = 60.0


class SmokeGuardClient:
    def __init__(self, inner: LLMClient, *, forbidden_values: list[str]) -> None:
        self._inner = inner
        self._forbidden = [v for v in forbidden_values if v]
        self.requests: list[LLMRequest] = []
        self.responses: list[LLMResponse] = []
        self.tokens_used = 0
        self.refusal: str | None = None

    @property
    def calls(self) -> int:
        return len(self.requests)

    def create(self, request: LLMRequest) -> LLMResponse:
        if "submit_analysis" not in {t["name"] for t in request.tools}:
            return self._refuse("smoke test complete: it stops after the analysis step")
        if self.calls >= MAX_CALLS:
            return self._refuse(f"smoke test limit reached: {MAX_CALLS} API calls")
        if self.calls >= 1 and self.tokens_used > SECOND_CALL_MAX_TOKENS_USED:
            return self._refuse(
                f"smoke test: the first call used {self.tokens_used} tokens, too many to retry within {TOKEN_LIMIT}"
            )
        serialized = json.dumps(dataclasses.asdict(request))
        if any(value in serialized for value in self._forbidden):
            return self._refuse("smoke test refused to send: a configured secret appeared in the request")
        self.requests.append(request)
        response = self._inner.create(request)
        self.responses.append(response)
        self.tokens_used += response.usage.input_tokens + response.usage.output_tokens
        return response

    def _refuse(self, reason: str) -> LLMResponse:
        # STOPPED, not CONFIG: the run pauses as "llm_stopped" and no error is recorded.
        self.refusal = reason
        raise LLMError(LLMErrorKind.STOPPED, reason)


class CaptureOnlyClient:
    """Dry run: records the request that would be sent, never sends it."""

    def create(self, request: LLMRequest) -> LLMResponse:
        raise LLMError(LLMErrorKind.STOPPED, "dry run: the request was not sent")


def smoke_settings(data_dir: Path, env_file: Path = REPO_ROOT / ".env") -> Settings:
    """OpenAI settings from .env; every limit is forced here, whatever .env says."""
    return Settings(
        _env_file=env_file if env_file.is_file() else None,  # type: ignore[call-arg]
        devpilot_api_token=secrets.token_urlsafe(32),  # throwaway, local only
        database_url=f"sqlite:///{(data_dir / 'smoke.db').as_posix()}",
        workspaces_dir=data_dir / "workspaces",
        log_level="WARNING",
        max_turns=MAX_CALLS,
        max_tool_calls=4,
        max_nudges=0,
        max_repeated_tool_calls=1,
        run_max_tokens=TOKEN_LIMIT,
        run_budget_usd=0.25,
        run_wall_clock_seconds=300,
        openai_max_output_tokens=MAX_OUTPUT_TOKENS,
        openai_max_retries=0,
        openai_timeout_seconds=TIMEOUT_SECONDS,
    )


def run_smoke(
    settings: Settings, inner: LLMClient, emit: Callable[[str], None] = print
) -> dict[str, Any]:
    """Run the smoke scenario through the real API. Returns a summary without secrets."""
    from app.main import create_app

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=".*httpx.*", category=DeprecationWarning)
        from fastapi.testclient import TestClient

    guard = SmokeGuardClient(inner, forbidden_values=settings.secret_values())
    app = create_app(settings, llm=guard)
    headers = {"Authorization": f"Bearer {settings.devpilot_api_token.get_secret_value()}"}
    api = "/api/v1"
    with TestClient(app) as client:
        project = client.post(
            f"{api}/projects", json={"name": PROJECT_NAME, "requirement_text": REQUIREMENT}, headers=headers
        ).json()
        run = client.post(f"{api}/projects/{project['id']}/runs", headers=headers).json()
        client.post(f"{api}/runs/{run['id']}/start", json={"approved_max_usd": settings.run_budget_usd}, headers=headers).raise_for_status()
        deadline = time.monotonic() + TIMEOUT_SECONDS * MAX_CALLS + 30
        while time.monotonic() < deadline:
            run = client.get(f"{api}/runs/{run['id']}", headers=headers).json()
            if run["pause_reason"] or run["ended_at"] or run["state"] == "AWAITING_APPROVAL":
                break
            time.sleep(0.1)
        invocations = client.get(f"{api}/runs/{run['id']}/invocations", headers=headers).json()
        analysis = client.get(f"{api}/runs/{run['id']}/analysis", headers=headers)
        events = client.get(f"{api}/runs/{run['id']}/events?limit=500", headers=headers).json()
        llm_errors = [e["payload"] for e in events if e["type"] == "llm.error"]
        llm_stops = [e["payload"] for e in events if e["type"] == "llm.stopped"]

    summary: dict[str, Any] = {
        "model": settings.openai_model,
        "api_calls": guard.calls,
        "tokens_per_call": [r.usage.input_tokens + r.usage.output_tokens for r in guard.responses],
        "tokens_total": guard.tokens_used,
        "cost_known": run["cost_known"],
        "cost_usd": run["cost_usd"] if run["cost_known"] else None,
        "state": run["state"],
        "pause_reason": run["pause_reason"],
        "outcome": run["outcome"],
        "invocations": [(i["tool"], i["ok"], i["error_class"]) for i in invocations],
        "analysis_accepted": analysis.status_code == 200,
        "requirements": [r["code"] + " " + r["text"] for r in analysis.json()["requirements"]] if analysis.status_code == 200 else [],
        "guard_stop": guard.refusal,
        "llm_stops": llm_stops,  # deliberate stops (not faults); a real error is only ever in llm_errors
        "llm_errors": llm_errors,
        "requests": guard.requests,
    }
    return summary


def describe_request(request: LLMRequest, settings: Settings) -> list[str]:
    """Human-readable view of one request, as the OpenAI client would send it."""
    tools_json = json.dumps(request.tools)
    input_json = json.dumps(request.input)
    approx_tokens = (len(request.instructions) + len(input_json) + len(tools_json)) // 4
    lines = [
        "POST https://api.openai.com/v1/responses" if not settings.openai_base_url else f"POST {settings.openai_base_url}/responses",
        f"  model:               {settings.openai_model or '<OPENAI_MODEL not set>'}",
        f"  previous_response_id: {request.previous_response_id}",
        "  store: true   parallel_tool_calls: false   tool_choice: required",
        f"  max_output_tokens:   {settings.openai_max_output_tokens}",
        f"  tools (strict):      {', '.join(t['name'] for t in request.tools)}  ({len(tools_json)} chars of schema)",
        f"  instructions:        {len(request.instructions)} chars",
        f"  input items:         {len(request.input)}",
        f"  estimated input:     ~{approx_tokens} tokens (characters / 4; the model's tokenizer decides the real count)",
    ]
    for item in request.input:
        if item.get("role") == "user":
            lines.append("  user message:")
            lines.extend(f"    | {line}" for line in item["content"].splitlines())
    return lines


def default_data_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="devpilot-smoke-"))
