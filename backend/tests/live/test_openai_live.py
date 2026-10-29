"""Opt-in live smoke test against the real OpenAI API. COSTS MONEY; needs explicit approval.

Skipped unless RUN_LIVE_OPENAI_TESTS=true is set in the environment. It reads the key
and model from the repo-root .env and makes at most two small GPT calls: one analysis
turn for the calculator requirement (MAX_TURNS=2 caps it). The key is never printed.

    $env:RUN_LIVE_OPENAI_TESTS = 'true'
    cd backend; & $env:DEVPILOT_PYTHON -m pytest tests/live -m live_openai -s
    Remove-Item Env:RUN_LIVE_OPENAI_TESTS
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import REPO_ROOT, Settings
from app.core.egress import EgressPolicy
from app.db.models import RunState
from app.orchestrator.llm_client import OpenAIResponsesClient, llm_config_problem
from tests.harness import Harness

pytestmark = pytest.mark.live_openai


def test_one_real_function_calling_turn(tmp_path: Path) -> None:
    real = Settings(_env_file=REPO_ROOT / ".env")  # type: ignore[call-arg]
    problem = llm_config_problem(real, EgressPolicy.from_file(real.egress_allowlist_file))
    if problem:
        pytest.skip(f"OpenAI not configured: {problem}")

    h = Harness(
        tmp_path,
        openai_api_key=real.openai_api_key.get_secret_value(),  # type: ignore[union-attr]
        openai_model=real.openai_model,
        openai_base_url=real.openai_base_url,
        openai_price_input_per_mtok=real.openai_price_input_per_mtok,
        openai_price_output_per_mtok=real.openai_price_output_per_mtok,
        max_turns=2,
        max_nudges=0,
        run_max_tokens=60_000,
    )
    h.llm = OpenAIResponsesClient(h.settings)
    run_id = h.new_run()
    run = h.advance(run_id)

    invocations = h.invocations(run_id)
    print(f"\nmodel={real.openai_model} state={run.state} turns={run.turns} tool_calls={run.tool_calls} "
          f"tokens_in={run.tokens_in} tokens_out={run.tokens_out} cost_known={run.cost_known} cost_usd={run.cost_usd:.5f}")
    for inv in invocations:
        out = json.loads(inv.output_json or "{}")
        print(f"  {inv.tool}: ok={inv.ok} error={inv.error_class} {(out.get('error') or {}).get('details', '')}")
    assert run.turns >= 1
    assert invocations, "GPT returned no function call"
    assert invocations[0].tool in {"submit_analysis", "request_human_input"}
    assert run.state in {RunState.PLANNING, RunState.DEFINING_CRITERIA, RunState.ANALYZING, RunState.AWAITING_APPROVAL, RunState.STOPPED_BY_POLICY}
    h.close()
