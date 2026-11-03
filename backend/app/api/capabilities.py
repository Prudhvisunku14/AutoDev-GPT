"""What this backend can build right now, and why not when it cannot: GPT, the coding engine, the sandbox, prices, stacks.

Every answer comes from the running configuration: the GPT client's own check, each coding provider's
`unavailable_reason()` (configuration only: nothing is started), the sandbox the app was given, and each stack's
verification recipe (`verification.recipes.readiness`). A stack is verifiable only when its recipe and runtime image are
approved; a stack that is not is still offered for planning, and the dashboard says it cannot be built.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from app.coding.launcher import config_problems
from app.core.auth import require_api_token
from app.schemas.api import CapabilitiesOut, PartOut, StackOut
from app.verification.recipes import readiness

router = APIRouter(tags=["capabilities"], dependencies=[Depends(require_api_token)])

REAL_SANDBOX = "DockerSandbox"


@router.get("/capabilities", response_model=CapabilitiesOut)
def get_capabilities(request: Request) -> CapabilitiesOut:
    state = request.app.state
    s = state.settings
    problem = state.llm_problem()
    gpt = PartOut(ready=problem is None, label=state.gpt_label, detail=problem, simulated=state.gpt_label.startswith("FakeLLM"))
    coding = _coding(state.coding_selector, config_problems(s, state.egress))
    sandbox_kind = type(state.sandbox).__name__ if state.sandbox is not None else None
    sandbox = PartOut(
        ready=state.sandbox is not None,
        label=sandbox_kind or "No sandbox",
        detail=None if state.sandbox is not None else "No sandbox is configured, so nothing could run the tests",
        simulated=sandbox_kind is not None and sandbox_kind != REAL_SANDBOX,
    )
    prices = PartOut(
        ready=s.cost_known,
        label=f"${s.openai_price_input_per_mtok} in / ${s.openai_price_output_per_mtok} out per 1M tokens" if s.cost_known else "Prices not set",
        detail=None if s.cost_known else "OPENAI_PRICE_INPUT_PER_MTOK and OPENAI_PRICE_OUTPUT_PER_MTOK are not set, so no spending limit can be enforced",
        simulated=False,
    )
    stacks = []
    for stack_id in state.catalog.ids:
        entry = state.catalog.get(stack_id)
        ok, why = readiness(stack_id)
        if ok and state.sandbox is None:
            ok, why = False, "no sandbox is configured"
        stacks.append(StackOut(id=entry.id, name=entry.name, language=entry.language, frameworks=list(entry.frameworks), description=entry.description, verifiable=ok, reason=why))
    parts = {"GPT": gpt, "the coding engine": coding, "the sandbox": sandbox, "prices": prices}
    problems = [f"{name}: {part.detail or 'not ready'}" for name, part in parts.items() if not part.ready]
    if not any(stack.verifiable for stack in stacks):
        problems.append("no stack can be verified")
    history = s.history_dir is not None and s.history_dir.is_dir()
    return CapabilitiesOut(
        gpt=gpt,
        coding=coding,
        sandbox=sandbox,
        prices=prices,
        stacks=stacks,
        can_build=not problems,
        problems=problems,
        simulated=[name for name, part in (("GPT", gpt), ("coding", coding), ("tests", sandbox)) if part.simulated],
        acceptance_suites=True,
        history=history,
    )


def _coding(selector: Any, problems: list[str]) -> PartOut:
    """`problems`: what the OpenCode launcher's own check finds missing in this backend's settings (names, never values)."""
    if selector is None:
        detail = (
            "The OpenCode coding engine is not connected to this backend, so a run would plan and then pause before writing code. Still needed: "
            + "; ".join(problems)
            if problems
            else "The settings are complete, but this backend was started without the coding engine: start it with scripts/live_server.py"
        )
        return PartOut(ready=False, label="No coding engine", detail=detail, simulated=False)
    options = tuple(getattr(selector, "options", ()))
    if not options:
        return PartOut(ready=True, label="configured", detail=None, simulated=False)
    reasons = {option.name: option.launcher.unavailable_reason() for option in options}
    available = [option for option in options if reasons[option.name] is None]
    return PartOut(
        ready=bool(available),
        label=", ".join(option.name for option in options),
        detail=None if available else "; ".join(f"{name}: {why}" for name, why in reasons.items()),
        simulated=bool(available) and all(option.simulated for option in available),
    )
