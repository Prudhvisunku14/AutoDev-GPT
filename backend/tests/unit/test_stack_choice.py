"""Phase 4: no stack is guessed from keywords. A project may be created without one; GPT chooses from the catalog while
planning, and the backend refuses to start a full build when it could verify no stack at all."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.egress import EgressPolicy
from app.demo.competition import _simulated
from app.db.models import RunState
from app.main import create_app
from app.orchestrator.fake_llm import FakeLLM
from app.orchestrator.prompts import instructions_for
from app.sandbox.replay import ReplaySandbox
from app.selection.catalog import build_selector
from app.stacks.catalog import StackCatalog
from tests.helpers import ALLOWLIST_FILE, API_TOKEN, make_settings
from tests.unit.test_coding_tools import pause

HEADERS = {"Authorization": f"Bearer {API_TOKEN}"}


def full_build_app(tmp_path: Path) -> Any:
    settings = make_settings(tmp_path, log_level="ERROR", openai_price_input_per_mtok=1.0, openai_price_output_per_mtok=1.0)
    selector = build_selector(settings, EgressPolicy.from_file(ALLOWLIST_FILE), [_simulated("simulated", [])])
    return create_app(settings, llm=FakeLLM(pause()), sandbox=ReplaySandbox.load(), coding=selector)


def start(client: TestClient, requirement: str) -> Any:
    project = client.post("/api/v1/projects", headers=HEADERS, json={"name": "App", "requirement_text": requirement}).json()
    assert project["stack"] is None  # nothing was chosen for GPT
    run = client.post(f"/api/v1/projects/{project['id']}/runs", headers=HEADERS).json()
    limit = client.get("/api/v1/budget", headers=HEADERS).json()["run_limit_usd"]
    return client.post(f"/api/v1/runs/{run['id']}/start", headers=HEADERS, json={"approved_max_usd": limit})


def test_a_project_without_a_stack_starts_when_some_stack_is_verifiable(tmp_path: Path) -> None:
    with TestClient(full_build_app(tmp_path)) as client:
        assert any(s["verifiable"] for s in client.get("/api/v1/capabilities", headers=HEADERS).json()["stacks"])
        assert start(client, "Create a homepage for my bakery with opening hours").status_code == 202


def test_a_full_build_with_no_verifiable_stack_is_refused_before_anything_is_spent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.api.capabilities as capabilities

    monkeypatch.setattr(capabilities, "readiness", lambda stack: (False, f"{stack} runtime is not approved here"))
    with TestClient(full_build_app(tmp_path)) as client:
        answer = start(client, "Create a homepage for my bakery with opening hours")
    assert answer.status_code == 409
    assert "No kind of application can be built and verified on this backend yet" in answer.json()["detail"]
    assert "python-cli runtime is not approved here" in answer.json()["detail"]


def test_gpt_sees_which_stacks_can_be_built_now_when_it_chooses() -> None:
    described = StackCatalog.load().describe()
    assert "python-cli" in described and "can be built and tested now: yes" in described and "can be built and tested now: no" in described
    prompt = instructions_for(RunState.PLANNING, catalog=StackCatalog.load(), requirements=[("R-001", "a homepage")], stack=None)
    assert "can be built and tested now: yes" in prompt  # GPT plans with the same readiness the backend enforces
