"""The spending picture a person needs before approving a build: the limits that are enforced, and what was spent.

Nothing here is an estimate. A run's cost depends on how GPT plans and how many coding and repair attempts it needs, so
the next run's cost is not predicted. What is certain is the limit, enforced before every model request
(app/orchestrator/budget.py), and what earlier runs on this database recorded.
"""

from __future__ import annotations

import statistics

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from app.api.deps import SessionDep
from app.core.auth import require_api_token
from app.db.models import Run, RunState
from app.schemas.api import BudgetOut, ReferenceCostOut, SpendingOut

router = APIRouter(tags=["budget"], dependencies=[Depends(require_api_token)])


@router.get("/budget", response_model=BudgetOut)
def get_budget(request: Request, session: SessionDep) -> BudgetOut:
    s = request.app.state.settings
    limit = s.run_budget_usd
    runs = list(session.scalars(select(Run)))
    known = [r for r in runs if r.cost_known]
    active = [r for r in runs if not r.is_terminal and r.state is not RunState.CREATED]
    finished = sorted(r.cost_usd for r in runs if r.is_terminal and r.cost_known and r.started_at is not None)
    reference = ReferenceCostOut(
        finished_runs=len(finished),
        min_usd=finished[0] if finished else None,
        median_usd=statistics.median(finished) if finished else None,
        max_usd=finished[-1] if finished else None,
    )
    policy = request.app.state.start_policy
    return BudgetOut(
        cost_known=s.cost_known,
        run_limit_usd=limit,
        coding_attempt_limit_usd=s.gateway_max_usd_per_task,
        model=s.openai_model,
        price_input_per_mtok=s.openai_price_input_per_mtok,
        price_output_per_mtok=s.openai_price_output_per_mtok,
        max_turns=s.max_turns,
        max_tool_calls=s.max_tool_calls,
        max_repair_attempts=s.max_repair_attempts,
        run_wall_clock_seconds=s.run_wall_clock_seconds,
        approval_required=s.require_spend_approval,
        session=policy.describe(session) if policy is not None else None,
        recorded=SpendingOut(
            total_usd=round(sum(r.cost_usd for r in known), 6),
            runs=len(runs),
            unknown_cost_runs=len(runs) - len(known),
            active_runs=len(active),
            active_committed_usd=round(sum(max(limit, r.cost_usd) for r in active), 6),
        ),
        reference=reference,
        estimate=_estimate(s.cost_known, limit, reference),
    )


def _estimate(cost_known: bool, limit: float, reference: ReferenceCostOut) -> str:
    if not cost_known:
        return ("Prices are not set (OPENAI_PRICE_INPUT_PER_MTOK, OPENAI_PRICE_OUTPUT_PER_MTOK), so a run's cost can be neither calculated nor "
                "limited. A build cannot be approved until they are set.")  # fmt: skip
    text = (f"The cost of a build cannot be estimated reliably: it depends on how GPT plans and how many coding and repair attempts the "
            f"build needs. What is certain is the limit: the run is stopped before any request that could take it past ${limit:.2f}, "
            f"GPT and the coding engine together.")  # fmt: skip
    if reference.finished_runs and reference.min_usd is not None and reference.max_usd is not None:
        text += f" Earlier finished runs on this backend cost ${reference.min_usd:.4f} to ${reference.max_usd:.4f} ({reference.finished_runs} runs)."
    return text
