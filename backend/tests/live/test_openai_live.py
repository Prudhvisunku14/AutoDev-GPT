"""Opt-in live smoke test against the real OpenAI API. COSTS MONEY; needs explicit approval.

Skipped unless RUN_LIVE_OPENAI_TESTS=true is set in the environment. It reads the key
and model from the repo-root .env and runs `app.demo.live_smoke` (at most 2 calls,
8,000 tokens, stops after the analysis). The key is never printed.

    $env:RUN_LIVE_OPENAI_TESTS = 'true'
    cd backend; & $env:DEVPILOT_PYTHON -m pytest tests/live -s
    Remove-Item Env:RUN_LIVE_OPENAI_TESTS
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.egress import EgressPolicy
from app.demo.live_smoke import MAX_CALLS, TOKEN_LIMIT, run_smoke, smoke_settings
from app.orchestrator.llm_client import OpenAIResponsesClient, llm_config_problem

pytestmark = pytest.mark.live_openai


def test_gpt_submits_a_structured_analysis(tmp_path: Path) -> None:
    settings = smoke_settings(tmp_path)
    problem = llm_config_problem(settings, EgressPolicy.from_file(settings.egress_allowlist_file))
    if problem:
        pytest.skip(f"OpenAI not configured: {problem}")

    summary = run_smoke(settings, OpenAIResponsesClient(settings))
    print({k: v for k, v in summary.items() if k != "requests"})

    assert 1 <= summary["api_calls"] <= MAX_CALLS
    assert summary["tokens_total"] <= TOKEN_LIMIT
    assert summary["llm_errors"] == []
    assert summary["invocations"], "GPT returned no function call"
    assert summary["invocations"][0][0] in {"submit_analysis", "request_human_input"}
    assert summary["analysis_accepted"], f"analysis not accepted: {summary['invocations']}"
    assert summary["state"] == "PLANNING"
    assert summary["pause_reason"] == "llm_stopped", "the run should end at the smoke guard, not at an error"
