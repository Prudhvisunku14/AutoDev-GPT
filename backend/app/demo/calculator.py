"""Offline Phase 2 demonstration: "Create a Python calculator application."

GPT is replaced by FakeLLM with a scripted, realistic sequence of function calls, so the
demo costs nothing and is reproducible. Everything else is the real backend: the REST
API, the orchestrator loop, the gate validation, the database and the event log.

The workflow stops at IMPLEMENTING, which needs Phase 3 (sandbox) and Phase 4
(OpenCode). No calculator code is generated, executed or tested.
"""

from __future__ import annotations

import secrets
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.orchestrator.fake_llm import FakeLLM, call

REQUIREMENT = "Create a Python calculator application."

ANALYSIS: dict[str, Any] = {
    "summary": "A command-line calculator written in Python that performs basic arithmetic on two numbers.",
    "requirements": [
        {"code": "R-001", "text": "Add, subtract, multiply and divide two numbers.", "kind": "functional"},
        {"code": "R-002", "text": "Report a clear error instead of crashing when dividing by zero.", "kind": "functional"},
        {"code": "R-003", "text": "Run from the command line as: calc <operation> <a> <b>.", "kind": "functional"},
        {"code": "R-004", "text": "Reject non-numeric operands with a helpful message.", "kind": "functional"},
        {"code": "R-005", "text": "Ship with automated pytest tests.", "kind": "non_functional"},
    ],
    "assumptions": ["Operands are decimal numbers.", "No graphical interface is needed.", "Python 3.12 is the target."],
    "ambiguities": [],
}

_PY = {"language": "python", "framework": None, "multi_file": True, "size": "small"}

PLAN: dict[str, Any] = {
    "stack": "python-cli",
    "stack_rationale": "A calculator used from a terminal needs no web server; python-cli is the smallest fitting stack.",
    "summary": "Pure arithmetic functions in one module, a thin argparse CLI on top, and pytest unit tests.",
    "tasks": [
        {
            "title": "Arithmetic core",
            "description": "Module calculator/core.py with add, subtract, multiply and divide; divide raises a clear error on zero.",
            "capability": "code_generation",
            "requirement_refs": ["R-001", "R-002"],
            "features": _PY,
        },
        {
            "title": "Command-line interface",
            "description": "calculator/cli.py parses operation and operands, validates numbers and prints the result or an error.",
            "capability": "code_generation",
            "requirement_refs": ["R-003", "R-004"],
            "features": _PY,
        },
        {
            "title": "Unit tests",
            "description": "pytest tests for every operation, division by zero, invalid input and the CLI exit codes.",
            "capability": "test_generation",
            "requirement_refs": ["R-001", "R-002", "R-003", "R-004", "R-005"],
            "features": _PY,
        },
    ],
}

CRITERIA: dict[str, Any] = {
    "criteria": [
        {
            "code": "AC-001",
            "given": "the calculator core module",
            "when": "add(2, 3), subtract(5, 2), multiply(4, 2.5) and divide(9, 3) are called",
            "then": "they return 5, 3, 10.0 and 3.0",
            "priority": "MUST",
            "method": "unit",
            "requirement_refs": ["R-001"],
        },
        {
            "code": "AC-002",
            "given": "the calculator core module",
            "when": "divide(1, 0) is called",
            "then": "a ZeroDivisionError with the message 'division by zero' is raised",
            "priority": "MUST",
            "method": "unit",
            "requirement_refs": ["R-002"],
        },
        {
            "code": "AC-003",
            "given": "the CLI",
            "when": "it runs with arguments 'add 2 3'",
            "then": "it prints 5 and exits with code 0",
            "priority": "MUST",
            "method": "unit",
            "requirement_refs": ["R-003"],
        },
        {
            "code": "AC-004",
            "given": "the CLI",
            "when": "it runs with arguments 'add two 3'",
            "then": "it prints an error naming the invalid operand and exits with code 2",
            "priority": "MUST",
            "method": "unit",
            "requirement_refs": ["R-004"],
        },
        {
            "code": "AC-005",
            "given": "the generated project",
            "when": "pytest is collected",
            "then": "at least one test exists for each of AC-001 to AC-004",
            "priority": "SHOULD",
            "method": "static",
            "requirement_refs": ["R-005"],
        },
    ]
}


def calculator_llm() -> FakeLLM:
    """GPT's scripted side of the conversation, including one rejected plan."""
    flawed_plan = {**PLAN, "stack": "django"}  # not in the catalog: the backend must refuse it
    return FakeLLM(
        call("submit_analysis", ANALYSIS),
        call("submit_plan", flawed_plan),
        call("submit_plan", PLAN),
        call("submit_criteria", CRITERIA),
    )


def run_demo(emit: Callable[[str], None] = print, data_dir: Path | None = None) -> dict[str, Any]:
    """Run the scenario through the real API with FakeLLM. Returns the final run JSON."""
    import warnings

    with warnings.catch_warnings():
        # Starlette's notice about its test client's HTTP library; irrelevant to the demo.
        warnings.filterwarnings("ignore", message=".*httpx.*", category=DeprecationWarning)
        from fastapi.testclient import TestClient

    from app.config import Settings
    from app.main import create_app

    data_dir = data_dir or Path(tempfile.mkdtemp(prefix="devpilot-demo-"))
    settings = Settings(
        _env_file=None,  # never read the real .env: no real key is involved
        devpilot_api_token=secrets.token_urlsafe(32),
        database_url=f"sqlite:///{(data_dir / 'demo.db').as_posix()}",
        workspaces_dir=data_dir / "workspaces",
        openai_price_input_per_mtok=1.0,
        openai_price_output_per_mtok=1.0,
        log_level="WARNING",
    )
    llm = calculator_llm()
    app = create_app(settings, llm=llm)
    headers = {"Authorization": f"Bearer {settings.devpilot_api_token.get_secret_value()}"}
    api = "/api/v1"

    with TestClient(app) as client:
        project = client.post(
            f"{api}/projects", json={"name": "Calculator", "requirement_text": REQUIREMENT}, headers=headers
        ).json()
        run = client.post(f"{api}/projects/{project['id']}/runs", headers=headers).json()
        emit(f'User: "{REQUIREMENT}"')
        emit(f"Project {project['id']}  run {run['id']}  state {run['state']}")
        client.post(f"{api}/runs/{run['id']}/start", json={"approved_max_usd": settings.run_budget_usd}, headers=headers).raise_for_status()

        deadline = time.monotonic() + 30
        while True:
            run = client.get(f"{api}/runs/{run['id']}", headers=headers).json()
            if run["pause_reason"] or run["ended_at"] or time.monotonic() > deadline:
                break
            time.sleep(0.05)

        analysis = client.get(f"{api}/runs/{run['id']}/analysis", headers=headers).json()
        plan = client.get(f"{api}/runs/{run['id']}/plan", headers=headers).json()
        criteria = client.get(f"{api}/runs/{run['id']}/criteria", headers=headers).json()
        invocations = client.get(f"{api}/runs/{run['id']}/invocations", headers=headers).json()
        events = client.get(f"{api}/runs/{run['id']}/events?limit=500", headers=headers).json()

    emit("\n1. GPT analyzed the request (submit_analysis accepted):")
    emit(f"   {analysis['summary']}")
    for r in analysis["requirements"]:
        emit(f"   {r['code']} [{r['kind']}] {r['text']}")
    emit(f"   assumptions: {'; '.join(analysis['assumptions'])}")

    emit(f"\n2. GPT selected the stack: {plan['stack']} - {plan['stack_rationale']}")
    emit("\n3. Development plan (submit_plan accepted on the second attempt):")
    for t in plan["tasks"]:
        emit(f"   {t['order']}. {t['title']} [{t['capability']}] covers {', '.join(t['requirement_refs'])}")

    emit("\n4. Acceptance criteria (submit_criteria accepted):")
    for c in criteria:
        emit(f"   {c['code']} {c['priority']:<6} {c['method']:<6} given {c['given']}, when {c['when']}, then {c['then']}")

    emit("\n5. Python validated every call before storing it:")
    for inv in invocations:
        verdict = "accepted" if inv["ok"] else f"REJECTED ({inv['error_class']})"
        emit(f"   #{inv['seq']} {inv['tool']:<16} in {inv['state_at_call']:<17} -> {verdict}")
        if not inv["ok"]:
            for detail in (inv["output"]["error"].get("details") or [])[:2]:
                emit(f"        {detail}")

    emit(f"\n6. Event log ({len(events)} events):")
    emit("   " + ", ".join(e["type"] for e in events))

    emit(f"\n7. Final state: {run['state']} (paused: {run['pause_reason']})")
    emit(f"   {run['status_detail']}")
    emit(
        f"   GPT turns: {run['turns']}, tool calls: {run['tool_calls']}, tokens: {run['tokens_in'] + run['tokens_out']} "
        f"(scripted), cost known: {run['cost_known']}"
    )
    emit("\nNo calculator code was generated, executed or tested: that needs Phase 3 and Phase 4.")
    return run
