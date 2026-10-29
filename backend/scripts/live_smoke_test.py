"""Live GPT smoke test. Default is a DRY RUN: it shows the request and sends nothing.

Run from the repo root after `. .\\scripts\\dev-env.ps1`:
    & $env:DEVPILOT_PYTHON backend\\scripts\\live_smoke_test.py          # dry run, free
    & $env:DEVPILOT_PYTHON backend\\scripts\\live_smoke_test.py --send   # PAID: at most 2 OpenAI calls

Never prints the API key or any other secret.
"""

from __future__ import annotations

import argparse
import sys

from pydantic import ValidationError

from app.core.egress import EgressPolicy
from app.demo.live_smoke import (
    MAX_CALLS,
    MAX_OUTPUT_TOKENS,
    REQUIREMENT,
    SECOND_CALL_MAX_TOKENS_USED,
    TOKEN_LIMIT,
    CaptureOnlyClient,
    default_data_dir,
    describe_request,
    run_smoke,
    smoke_settings,
)
from app.orchestrator.llm_client import OpenAIResponsesClient, llm_config_problem


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--send", action="store_true", help="really call OpenAI (costs money)")
    args = parser.parse_args()

    try:
        settings = smoke_settings(default_data_dir())
    except ValidationError as exc:
        print("The .env file has invalid values (values are not shown):")
        for err in exc.errors(include_input=False):
            print(f"  {'.'.join(str(p) for p in err['loc']).upper()}: {err['msg']}")
        return 1

    print(f'Requirement: "{REQUIREMENT}"')
    print(
        f"Limits: at most {MAX_CALLS} API calls, {TOKEN_LIMIT} tokens in total, {MAX_OUTPUT_TOKENS} output tokens per call, "
        f"second call only if the first used <= {SECOND_CALL_MAX_TOKENS_USED} tokens, SDK retries off, "
        "stops right after the analysis."
    )
    if settings.cost_known:
        assert settings.openai_price_input_per_mtok is not None and settings.openai_price_output_per_mtok is not None
        worst = (
            TOKEN_LIMIT * max(settings.openai_price_input_per_mtok, settings.openai_price_output_per_mtok) / 1_000_000
        )
        print(f"Worst-case cost at the prices in .env: ${worst:.4f} (all {TOKEN_LIMIT} tokens billed at the higher price)")
    else:
        print("Cost: unknown (OPENAI_PRICE_* not set in .env); the token limit applies.")

    if not args.send:
        summary = run_smoke(settings, CaptureOnlyClient(), emit=lambda _: None)
        (request,) = summary["requests"]
        print("\nDRY RUN - call 1 would be:")
        print("\n".join(describe_request(request, settings)))
        print("\nCall 2 happens only if call 1's analysis is rejected by validation. It sends the validation errors")
        print("as a function_call_output with previous_response_id = call 1's id, under the same output cap.")
        problem = llm_config_problem(settings, EgressPolicy.from_file(settings.egress_allowlist_file))
        print(f"\nReady to send: {'no - ' + problem if problem else 'yes'}. Nothing was sent.")
        return 0

    problem = llm_config_problem(settings, EgressPolicy.from_file(settings.egress_allowlist_file))
    if problem:
        print(f"Not configured: {problem}")
        return 1
    summary = run_smoke(settings, OpenAIResponsesClient(settings), emit=print)
    print(f"\nModel: {summary['model']}")
    print(f"API calls: {summary['api_calls']}  tokens per call: {summary['tokens_per_call']}  total: {summary['tokens_total']}")
    print(f"Cost: {'$%.5f' % summary['cost_usd'] if summary['cost_known'] else 'unknown (prices not set)'}")
    print(f"Tool calls: {summary['invocations']}")
    print(f"Analysis accepted by validation: {summary['analysis_accepted']}")
    for line in summary["requirements"]:
        print(f"  {line}")
    print(f"Run state: {summary['state']}  pause: {summary['pause_reason']}  outcome: {summary['outcome']}")
    if summary["guard_stop"]:
        print(f"Stopped on purpose by the smoke guard (not an error): {summary['guard_stop']}")
    for err in summary["llm_errors"]:
        print(f"OpenAI error ({err.get('kind')}): {err.get('message')}")
    return 0 if summary["analysis_accepted"] else 2


if __name__ == "__main__":
    sys.exit(main())
