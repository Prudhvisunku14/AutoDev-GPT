"""SessionBudget: the most one live server session's runs may spend together, counted without estimates."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import API_PREFIX, create_app
from app.orchestrator.fake_llm import FakeLLM
from app.orchestrator.start_policy import SessionBudget
from tests.helpers import API_TOKEN, make_settings

HEADERS = {"Authorization": f"Bearer {API_TOKEN}"}


class Runs:
    """What SessionBudget reads: session.get(Run, id)."""

    def __init__(self) -> None:
        self.rows: dict[str, Any] = {}

    def add(self, run_id: str, *, ended: bool = False, cost: float = 0.0, known: bool = True) -> Any:
        self.rows[run_id] = SimpleNamespace(id=run_id, is_terminal=ended, cost_usd=cost, cost_known=known)
        return self.rows[run_id]

    def get(self, _model: Any, run_id: str) -> Any:
        return self.rows.get(run_id)


def test_runs_are_admitted_until_one_more_could_pass_the_total() -> None:
    runs, budget = Runs(), SessionBudget(1.00, 0.50, run_seconds=60)
    assert budget.admit(runs, runs.add("r1")) is None  # type: ignore[arg-type]
    assert budget.admit(runs, runs.add("r2")) is None  # type: ignore[arg-type]
    refusal = budget.admit(runs, runs.add("r3"))  # type: ignore[arg-type]
    assert refusal is not None and "$1.00" in refusal and "$1.0000 is already counted" in refusal


def test_a_finished_run_counts_at_what_it_cost_and_frees_the_rest() -> None:
    runs, budget = Runs(), SessionBudget(1.00, 0.50, run_seconds=60)
    first, second = runs.add("r1"), runs.add("r2")
    budget.admit(runs, first)  # type: ignore[arg-type]
    budget.admit(runs, second)  # type: ignore[arg-type]
    first.is_terminal, first.cost_usd = True, 0.131854
    assert budget.admit(runs, runs.add("r3")) is not None  # 0.131854 + 0.50 (running) + 0.50 > 1.00
    second.is_terminal, second.cost_usd = True, 0.20
    assert budget.admit(runs, runs.add("r4")) is None  # 0.131854 + 0.20 + 0.50 fits
    assert budget.committed(runs) == pytest.approx(0.831854)  # type: ignore[arg-type]


def test_a_run_whose_cost_is_unknown_counts_at_its_whole_limit() -> None:
    runs, budget = Runs(), SessionBudget(0.50, 0.50, run_seconds=60)
    run = runs.add("r1")
    budget.admit(runs, run)  # type: ignore[arg-type]
    run.is_terminal, run.cost_usd, run.cost_known = True, 0.01, False
    assert budget.committed(runs) == 0.50  # type: ignore[arg-type]


def test_resuming_an_admitted_run_is_not_counted_twice() -> None:
    runs, budget = Runs(), SessionBudget(0.50, 0.50, run_seconds=60)
    run = runs.add("r1")
    assert budget.admit(runs, run) is None  # type: ignore[arg-type]
    assert budget.admit(runs, run) is None  # type: ignore[arg-type]
    assert budget.describe(runs)["runs_admitted"] == 1  # type: ignore[arg-type]


def test_no_run_starts_that_could_not_finish_before_the_session_ends() -> None:
    now = [0.0]
    runs, budget = Runs(), SessionBudget(5.00, 0.50, run_seconds=1_500, ends_at=3_600, clock=lambda: now[0])
    assert budget.admit(runs, runs.add("r1")) is None  # type: ignore[arg-type]
    now[0] = 2_200.0  # 1,400 s left, a run may need 1,500
    refusal = budget.admit(runs, runs.add("r2"))  # type: ignore[arg-type]
    assert refusal is not None and "session ends in" in refusal


def test_two_quick_starts_cannot_both_pass_the_check() -> None:
    runs, budget = Runs(), SessionBudget(1.00, 0.50, run_seconds=60)
    ids = [f"r{i}" for i in range(12)]
    for run_id in ids:
        runs.add(run_id)
    gate = threading.Barrier(len(ids))
    results: dict[str, str | None] = {}

    def start(run_id: str) -> None:
        gate.wait()
        results[run_id] = budget.admit(runs, runs.rows[run_id])  # type: ignore[arg-type]

    threads = [threading.Thread(target=start, args=(run_id,)) for run_id in ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(result is None for result in results.values()) == 2


def test_the_description_is_what_the_budget_endpoint_shows() -> None:
    runs, budget = Runs(), SessionBudget(1.00, 0.50, run_seconds=60)
    budget.admit(runs, runs.add("r1"))  # type: ignore[arg-type]
    assert budget.describe(runs) == {"approved_total_usd": 1.0, "committed_usd": 0.5, "remaining_usd": 0.5, "runs_admitted": 1, "seconds_left": None}  # type: ignore[arg-type]


def test_invalid_figures_are_refused() -> None:
    with pytest.raises(ValueError):
        SessionBudget(0, 0.5, run_seconds=60)


class Refuses:
    def admit(self, session: Any, run: Any) -> str | None:
        return "the session's approved total of $1.00 could be passed"

    def describe(self, session: Any) -> dict[str, Any]:
        return {"approved_total_usd": 1.0}


def test_the_api_refuses_a_start_the_policy_refuses_and_records_nothing(tmp_path: Any) -> None:
    settings = make_settings(tmp_path, openai_price_input_per_mtok=2.0, openai_price_output_per_mtok=10.0)
    with TestClient(create_app(settings, llm=FakeLLM(), start_policy=Refuses())) as client:
        project = client.post(f"{API_PREFIX}/projects", json={"name": "P", "requirement_text": "R"}, headers=HEADERS).json()
        run = client.post(f"{API_PREFIX}/projects/{project['id']}/runs", headers=HEADERS).json()
        answer = client.post(f"{API_PREFIX}/runs/{run['id']}/start", json={"approved_max_usd": settings.run_budget_usd}, headers=HEADERS)
        events = client.get(f"{API_PREFIX}/runs/{run['id']}/events", headers=HEADERS).json()
        budget = client.get(f"{API_PREFIX}/budget", headers=HEADERS).json()
    assert answer.status_code == 409 and "Not started" in answer.json()["detail"]
    assert [e["type"] for e in events] == ["run.created"]
    assert budget["session"] == {"approved_total_usd": 1.0}
