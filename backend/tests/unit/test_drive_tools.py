"""The Drive and RAG tools through the real API, orchestrator, approval queue and executor. **Simulated:** GPT is FakeLLM,
Google Drive is the fixture transport, consent is the approval, and the embedder is the hashing test double."""

from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app.core.egress import EgressPolicy
from app.core.events import event_payload, list_events
from app.db.models import Approval, ApprovalStatus, ToolInvocation
from app.demo import rag
from app.demo.competition import _simulated
from app.drive import fixtures
from app.drive.access import GoogleOAuthAuthorizer
from app.drive.model import DriveAuthorizationRequired
from app.drive.source import GoogleDriveSource
from app.main import create_app
from app.orchestrator import approvals
from app.orchestrator.fake_llm import FakeLLM, Step, call
from app.sandbox.replay import ReplaySandbox
from app.selection.catalog import build_selector
from app.tools.adapters.drive import DriveRuntime
from app.verification import acceptance
from tests.helpers import API_TOKEN, make_settings
from tests.unit.test_coding_tools import pause

with warnings.catch_warnings():
    warnings.filterwarnings("ignore", message=".*httpx.*")
    from fastapi.testclient import TestClient

GRANT = rag.GRANT
SUPPORTED = ["fxDocRagNotes001", "fxTxtChunking001", "fxDocxEval000001", "fxDocCitation001", fixtures.POISONED]
OPENING = [call("submit_analysis", rag.ANALYSIS), call("request_drive_authorization", {"folders": [fixtures.FOLDER_URL], "purpose": "Index the documents."}),
           call("discover_drive_documents", {"grant_id": GRANT}), call("ingest_drive_documents", {"grant_id": GRANT, "file_ids": SUPPORTED})]  # fmt: skip
INDEXED = [*OPENING, call("build_rag_index", {"chunk_chars": 800, "chunk_overlap": 120})]
DRIVE_TOOLS = {"request_drive_authorization", "discover_drive_documents", "ingest_drive_documents", "build_rag_index", "configure_retrieval", "prepare_rag_application", "revoke_drive_authorization"}


class DriveApp:
    """One app over the fixture Drive. Every approval is granted, as a person would after reading it."""

    def __init__(self, tmp_path: Path, steps: list[Step], runtime: DriveRuntime | None = None, *, embedders: bool = True) -> None:
        settings = make_settings(tmp_path, log_level="ERROR")
        self.drive = fixtures.FixtureDrive.research()
        if runtime is None:
            runtime, _source = rag.fixture_runtime(self.drive, settings)
            runtime.embedders = runtime.embedders if embedders else ()
        self.runtime = runtime
        selector = build_selector(settings, EgressPolicy.from_file(settings.egress_allowlist_file), [_simulated("simulated", [])])
        self.app = create_app(settings, llm=FakeLLM(*steps), sandbox=ReplaySandbox.load(), coding=selector, drive=runtime)
        self.client = TestClient(self.app)
        self.headers = {"Authorization": f"Bearer {API_TOKEN}"}
        self.approved: list[str] = []

    def start(self, name: str = "Chatbot", suite: acceptance.AcceptanceSuite | None = None) -> tuple[str, str]:
        body: dict[str, Any] = {"name": name, "requirement_text": rag.REQUIREMENT} | ({"acceptance": suite.to_dict()} if suite else {})
        project = self.client.post("/api/v1/projects", json=body, headers=self.headers).json()["id"]
        run_id = self.client.post(f"/api/v1/projects/{project}/runs", headers=self.headers).json()["id"]
        self.app.state.orchestrator.advance(run_id)
        while True:
            with self.app.state.session_factory.begin() as s:
                pending = s.scalars(select(Approval).where(Approval.run_id == run_id, Approval.status == ApprovalStatus.PENDING)).first()
                if pending is None:
                    break
                self.approved.append(pending.tool)
                approvals.decide(s, pending, decision="approve", response=None, executor=self.app.state.executor, events=self.app.state.events, redactor=self.app.state.redactor)  # type: ignore[arg-type]
            self.app.state.orchestrator.advance(run_id)
        return project, run_id

    def outputs(self, run_id: str, tool: str) -> list[dict[str, Any]]:
        with self.app.state.session_factory() as s:
            return [json.loads(i.output_json or "{}") for i in s.scalars(select(ToolInvocation).where(ToolInvocation.run_id == run_id, ToolInvocation.tool == tool).order_by(ToolInvocation.id))]

    def events(self, run_id: str, kind: str) -> list[dict[str, Any]]:
        with self.app.state.session_factory() as s:
            return [event_payload(e) for e in list_events(s, run_id=run_id, limit=5_000) if e.type == kind]

    def run(self, run_id: str) -> dict[str, Any]:
        return self.client.get(f"/api/v1/runs/{run_id}", headers=self.headers).json()

    def close(self) -> None:
        self.client.close()
        self.app.state.engine.dispose()


@pytest.fixture
def made(tmp_path: Path):  # noqa: ANN201
    apps: list[DriveApp] = []

    def build(steps: list[Step], **kwargs: Any) -> DriveApp:
        apps.append(DriveApp(tmp_path / str(len(apps)), steps, **kwargs))
        return apps[-1]

    yield build
    for app in apps:
        app.close()


def test_without_the_feature_no_drive_tool_exists(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path, log_level="ERROR"), llm=FakeLLM())
    try:
        assert not DRIVE_TOOLS & set(app.state.registry.names) and app.state.settings.drive_source == "off"
    finally:
        app.state.engine.dispose()


def test_the_offline_rehearsal_passes_every_check(tmp_path: Path) -> None:
    result = rag.rehearse(tmp_path)
    passed = rag.checks(result)
    assert all(passed.values()), [name for name, ok in passed.items() if not ok]
    text = rag.render(result, passed)
    assert "SIMULATED" in text and "No chatbot was generated" in text and "test double" in text


def test_the_frozen_acceptance_suite_binds_the_chat_criterion_to_the_front_end() -> None:
    acceptance.validate(rag.ACCEPTANCE_SUITE)
    assert rag.ACCEPTANCE_SUITE.components() == ("frontend",) and len(rag.ACCEPTANCE_SUITE.criteria) == 11
    assert any("not only a citation's presence" in source for source in rag.ACCEPTANCE_SUITE.files.values())  # grounding is checked, not just citations


def test_injected_text_is_flagged_never_shown_to_gpt_and_grants_nothing(made: Any) -> None:
    app = made([*OPENING, pause()])
    _, run_id = app.start()
    out = app.outputs(run_id, "ingest_drive_documents")[-1]["data"]
    assert out["flagged_for_instructions"] == [fixtures.POISONED] and out["ingested"] == 5
    seen = json.dumps([app.outputs(run_id, t) for t in DRIVE_TOOLS])
    assert "maintenance mode" not in seen and "Ignore all previous" not in seen and "roughly 800 characters" not in seen and "fixture-token" not in seen
    assert app.approved == ["request_drive_authorization", "discover_drive_documents", "ingest_drive_documents"]


def test_with_no_embedding_provider_the_recovery_engine_stops_the_run_by_policy(made: Any) -> None:
    app = made([*INDEXED, pause()], embedders=False)
    _, run_id = app.start()
    run = app.run(run_id)
    assert run["state"] == "STOPPED_BY_POLICY" and run["outcome"] == "embedding_unavailable" and "openai_embeddings" in run["status_detail"]
    decided = app.events(run_id, "recovery.decided")
    assert decided[-1]["for"] == "embedding" and decided[-1]["category"] == "TOOL_UNAVAILABLE" and decided[-1]["action"] == "STOP"
    assert app.events(run_id, "rag.indexed") == []


def test_revoking_access_removes_every_chunk_and_refuses_further_reads(made: Any) -> None:
    app = made([*INDEXED, call("revoke_drive_authorization", {"grant_id": GRANT, "reason": "The user withdrew consent."}), call("discover_drive_documents", {"grant_id": GRANT}), pause()])
    project, run_id = app.start()
    revoked = app.events(run_id, "drive.revoked")[-1]
    assert revoked["removed_chunks"] > 0 and revoked["removed_documents"] == 5
    assert app.outputs(run_id, "discover_drive_documents")[-1]["error"]["class"] == "POLICY_REFUSED"
    assert app.runtime.bases[project].index.chunks(app.runtime.scope(project)) == [] and fixtures.TOKENS[fixtures.RESEARCHER] not in app.runtime.authorizer._tokens.values()


def test_a_grant_cannot_be_used_by_another_project(made: Any) -> None:
    app = made([call("submit_analysis", rag.ANALYSIS), call("request_drive_authorization", {"folders": [fixtures.RESEARCH], "purpose": "Index."}), pause(),
                call("submit_analysis", rag.ANALYSIS), call("discover_drive_documents", {"grant_id": GRANT}), pause()])  # fmt: skip
    app.start("First")
    _, second = app.start("Second")
    refused = app.outputs(second, "discover_drive_documents")[-1]
    assert refused["error"]["class"] == "POLICY_REFUSED" and "for this project" in refused["error"]["summary"]


def test_the_plan_must_keep_the_blueprints_components_and_dependencies(made: Any) -> None:
    loose = {**rag.PLAN, "tasks": [{**t, "depends_on": []} if t["component"] == "frontend" and t["capability"] == "code_generation" else t for t in rag.PLAN["tasks"]]}
    no_knowledge = {**rag.PLAN, "components": rag.PLAN["components"][1:],
                    "tasks": [{**t, "component": "backend", "depends_on": [d for d in t["depends_on"] if d > 2]} if t["component"] == "knowledge" else t for t in rag.PLAN["tasks"]]}  # fmt: skip
    app = made([*INDEXED, call("prepare_rag_application", {"summary": "Answer from the documents."}), call("submit_plan", loose), call("submit_plan", no_knowledge), call("submit_plan", rag.PLAN), pause()])
    _, run_id = app.start()
    first, second, third = (o for o in app.outputs(run_id, "submit_plan"))
    assert "the RAG blueprint needs a frontend task that depends on a backend task" in first["error"]["details"]
    assert "the RAG blueprint needs a component 'knowledge' with stack 'python-fastapi'" in second["error"]["details"]
    assert third["ok"] and app.run(run_id)["state"] == "DEFINING_CRITERIA"
    blueprint = app.events(run_id, "rag.blueprint")[-1]
    assert [c["name"] for c in blueprint["components"]] == ["knowledge", "backend", "frontend"] and blueprint["blockers"]


def test_real_consent_stays_pending_and_nothing_can_be_read(made: Any, tmp_path: Path) -> None:
    oauth = GoogleOAuthAuthorizer("client-123.apps.googleusercontent.com", "http://127.0.0.1:8000/oauth/google/callback")
    source = GoogleDriveSource(EgressPolicy.from_file(make_settings(tmp_path).egress_allowlist_file), oauth)  # never called: consent never completes
    app = made([call("submit_analysis", rag.ANALYSIS), call("request_drive_authorization", {"folders": [fixtures.RESEARCH], "purpose": "Index."}), pause()],
               runtime=DriveRuntime(source, oauth))  # fmt: skip
    project, run_id = app.start()
    answer = app.outputs(run_id, "request_drive_authorization")[-1]["data"]
    assert answer["status"] == "PENDING_CONSENT" and "accounts.google.com" not in json.dumps(answer) and "state" not in answer  # the URL stays with DevPilot
    with pytest.raises(DriveAuthorizationRequired, match="pending consent"):
        app.runtime.grants.usable(answer["grant_id"], project)
