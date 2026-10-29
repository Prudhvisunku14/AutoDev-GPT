"""The live smoke test's guard and dry run, exercised offline with FakeLLM (no network, no cost)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.demo.calculator import ANALYSIS
from app.demo.live_smoke import (
    MAX_OUTPUT_TOKENS,
    REQUIREMENT,
    TOKEN_LIMIT,
    CaptureOnlyClient,
    SmokeGuardClient,
    describe_request,
    run_smoke,
    smoke_settings,
)
from app.orchestrator.fake_llm import FakeLLM, call, fail
from app.orchestrator.llm_client import FunctionCall, LLMError, LLMErrorKind, LLMRequest, LLMResponse, Usage
from tests.helpers import FAKE_OPENAI_KEY

BAD = {**ANALYSIS, "requirements": [{**ANALYSIS["requirements"][0], "code": "R-009"}]}


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    path = tmp_path / ".env"
    path.write_text(
        f"OPENAI_API_KEY={FAKE_OPENAI_KEY}\nOPENAI_MODEL=test-model\n"
        "MAX_TURNS=50\nRUN_MAX_TOKENS=1000000\nOPENAI_MAX_RETRIES=5\nOPENAI_MAX_OUTPUT_TOKENS=99999\n",
        encoding="utf-8",
    )
    return path


def _settings(tmp_path: Path, env_file: Path):  # noqa: ANN202
    return smoke_settings(tmp_path / "data", env_file=env_file)


def test_limits_are_forced_regardless_of_env(tmp_path: Path, env_file: Path) -> None:
    s = _settings(tmp_path, env_file)
    assert s.openai_model == "test-model"
    assert (s.max_turns, s.run_max_tokens, s.openai_max_retries) == (2, TOKEN_LIMIT, 0)
    assert s.openai_max_output_tokens == MAX_OUTPUT_TOKENS
    assert s.max_nudges == 0


def test_accepted_analysis_uses_one_call_and_stops(tmp_path: Path, env_file: Path) -> None:
    fake = FakeLLM(call("submit_analysis", ANALYSIS), call("submit_plan", {}))
    summary = run_smoke(_settings(tmp_path, env_file), fake)
    assert summary["api_calls"] == 1
    assert summary["analysis_accepted"] and len(summary["requirements"]) == 5
    assert summary["state"] == "PLANNING"
    assert summary["guard_stop"] == "smoke test complete: it stops after the analysis step"
    assert summary["llm_errors"] == []
    # The deliberate stop is a pause reason of its own, never reported as a config error.
    assert summary["pause_reason"] == "llm_stopped"
    assert [stop["message"] for stop in summary["llm_stops"]] == [summary["guard_stop"]]
    assert fake.remaining == 1  # the planning step was never requested
    (request,) = summary["requests"]
    assert f"<requirement>\n{REQUIREMENT}\n</requirement>" in request.input[0]["content"]


def test_rejected_analysis_gets_one_retry(tmp_path: Path, env_file: Path) -> None:
    fake = FakeLLM(call("submit_analysis", BAD), call("submit_analysis", ANALYSIS))
    summary = run_smoke(_settings(tmp_path, env_file), fake)
    assert summary["api_calls"] == 2
    assert summary["analysis_accepted"]
    assert [ok for _, ok, _ in summary["invocations"]] == [False, True]


def test_a_genuine_config_error_is_reported_as_one(tmp_path: Path, env_file: Path) -> None:
    fake = FakeLLM(fail(LLMErrorKind.CONFIG, "OPENAI_MODEL is not set for this key"))
    summary = run_smoke(_settings(tmp_path, env_file), fake)
    assert summary["pause_reason"] == "llm_error:config"
    assert [e["kind"] for e in summary["llm_errors"]] == ["CONFIG"]
    assert summary["llm_stops"] == [] and summary["guard_stop"] is None


def test_never_more_than_two_calls(tmp_path: Path, env_file: Path) -> None:
    second_bad = copy.deepcopy(BAD)
    second_bad["summary"] = "different"
    fake = FakeLLM(call("submit_analysis", BAD), call("submit_analysis", second_bad), call("submit_analysis", ANALYSIS))
    summary = run_smoke(_settings(tmp_path, env_file), fake)
    assert summary["api_calls"] == 2
    assert fake.remaining == 1
    assert not summary["analysis_accepted"]
    assert summary["outcome"] == "turn_limit"


def test_no_retry_after_an_expensive_first_call(tmp_path: Path, env_file: Path) -> None:
    def expensive(request: LLMRequest, n: int) -> LLMResponse:
        return LLMResponse(
            id=f"resp_{n}",
            function_calls=[FunctionCall(call_id="c1", name="submit_analysis", arguments=json.dumps(BAD))],
            usage=Usage(input_tokens=1_800, output_tokens=900),
        )

    fake = FakeLLM(expensive, call("submit_analysis", ANALYSIS))
    summary = run_smoke(_settings(tmp_path, env_file), fake)
    assert summary["api_calls"] == 1
    assert "too many to retry" in (summary["guard_stop"] or "")
    assert summary["tokens_total"] == 2_700


def test_request_containing_a_secret_is_never_sent() -> None:
    inner = FakeLLM(call("submit_analysis", ANALYSIS))
    guard = SmokeGuardClient(inner, forbidden_values=[FAKE_OPENAI_KEY])
    request = LLMRequest(
        instructions="i",
        input=[{"role": "user", "content": f"leak {FAKE_OPENAI_KEY}"}],
        tools=[{"name": "submit_analysis"}],
        previous_response_id=None,
    )
    with pytest.raises(LLMError, match="secret appeared") as info:
        guard.create(request)
    assert info.value.kind is LLMErrorKind.STOPPED
    assert inner.requests == [] and guard.calls == 0


def test_dry_run_sends_nothing_and_describes_the_request(tmp_path: Path, env_file: Path) -> None:
    settings = _settings(tmp_path, env_file)
    summary = run_smoke(settings, CaptureOnlyClient())
    assert summary["invocations"] == []
    assert summary["pause_reason"] == "llm_stopped" and summary["llm_errors"] == []
    (request,) = summary["requests"]
    assert [t["name"] for t in request.tools] == ["request_human_input", "submit_analysis"]
    assert all(t["strict"] for t in request.tools)
    text = "\n".join(describe_request(request, settings))
    assert "model:               test-model" in text
    assert f"max_output_tokens:   {MAX_OUTPUT_TOKENS}" in text
    assert REQUIREMENT in text
    assert FAKE_OPENAI_KEY not in text
    assert FAKE_OPENAI_KEY not in json.dumps([r.__dict__ for r in summary["requests"]], default=str)
