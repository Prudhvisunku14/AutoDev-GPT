"""Builds the real orchestrator stack (no HTTP) around a FakeLLM for unit tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import Field
from sqlalchemy import select

from app.core.events import EventRecorder, event_payload, list_events
from app.core.redaction import Redactor
from app.db import repositories
from app.db.models import Approval, Project, Run, RunState, ToolInvocation, new_id
from app.db.session import create_db_engine, create_session_factory, init_db
from app.orchestrator import approvals
from app.orchestrator.fake_llm import FakeLLM
from app.orchestrator.llm_client import LLMClient
from app.orchestrator.loop import Orchestrator
from app.stacks.catalog import StackCatalog
from app.tools.base import RiskLevel, ToolArgs, ToolContext, ToolSpec
from app.tools.executor import ToolExecutor
from app.tools.gates import register_phase2_tools
from app.tools.policy import PolicyEngine
from app.tools.registry import ToolRegistry
from tests.helpers import make_settings

CALCULATOR = "Create a Python calculator application."


class PublishArgs(ToolArgs):
    target: str = Field(min_length=1)


class Harness:
    """Real Settings, DB, registry, executor and orchestrator; GPT is a FakeLLM."""

    def __init__(self, tmp_path: Path, llm: LLMClient | None = None, *, risky_tool: bool = False, **overrides: Any) -> None:
        self.settings = make_settings(tmp_path, **overrides)
        self.engine = create_db_engine(self.settings)
        init_db(self.engine)
        self.sessions = create_session_factory(self.engine)
        self.redactor = Redactor(self.settings.secret_values())
        self.events = EventRecorder(self.redactor)
        self.catalog = StackCatalog.load()
        self.registry = ToolRegistry()
        register_phase2_tools(self.registry)
        self.published: list[str] = []
        if risky_tool:
            self.registry.register(
                ToolSpec(
                    name="publish_demo",
                    description="Test-only tool with GIT_PUSH risk.",
                    args_model=PublishArgs,
                    risk=RiskLevel.GIT_PUSH,
                    states=frozenset({RunState.ANALYZING}),
                    handler=self._publish,
                )
            )
        self.executor = ToolExecutor(
            self.registry, PolicyEngine(), self.redactor, max_output_chars=self.settings.tool_output_max_chars
        )
        self.llm = llm if llm is not None else FakeLLM()
        self.orchestrator = Orchestrator(
            settings=self.settings,
            session_factory=self.sessions,
            registry=self.registry,
            executor=self.executor,
            events=self.events,
            catalog=self.catalog,
            redactor=self.redactor,
            llm_provider=lambda: self.llm,
        )

    def _publish(self, ctx: ToolContext, args: PublishArgs) -> dict[str, object]:
        self.published.append(args.target)
        return {"published": args.target}

    def new_run(self, requirement: str = CALCULATOR, name: str = "Calculator") -> str:
        with self.sessions.begin() as s:
            project = repositories.create_project(
                s,
                project_id=new_id(),
                name=self.redactor.redact_text(name),
                requirement_text=self.redactor.redact_text(requirement),
                workspace_path="unused",
            )
            return repositories.create_run(s, project).id

    def advance(self, run_id: str) -> Run:
        self.orchestrator.advance(run_id)
        return self.run(run_id)

    def run(self, run_id: str) -> Run:
        with self.sessions() as s:
            run = s.get(Run, run_id)
            assert run is not None
            s.expunge(run)
            return run

    def project(self, run_id: str) -> Project:
        with self.sessions() as s:
            project = s.get(Project, self.run(run_id).project_id)
            assert project is not None
            s.expunge(project)
            return project

    def invocations(self, run_id: str) -> list[ToolInvocation]:
        with self.sessions() as s:
            rows = list(s.scalars(select(ToolInvocation).where(ToolInvocation.run_id == run_id).order_by(ToolInvocation.seq)))
            for r in rows:
                s.expunge(r)
            return rows

    def outputs(self, run_id: str) -> list[dict[str, Any]]:
        return [json.loads(i.output_json) for i in self.invocations(run_id) if i.output_json]

    def event_types(self, run_id: str) -> list[str]:
        with self.sessions() as s:
            return [e.type for e in list_events(s, run_id=run_id, limit=1000)]

    def events_of(self, run_id: str, type: str) -> list[dict[str, Any]]:
        with self.sessions() as s:
            return [event_payload(e) for e in list_events(s, run_id=run_id, limit=1000) if e.type == type]

    def pending_approval(self, run_id: str) -> Approval:
        with self.sessions() as s:
            approval = s.scalars(select(Approval).where(Approval.run_id == run_id)).one()
            s.expunge(approval)
            return approval

    def decide(self, approval_id: str, decision: str, response: str | None = None) -> None:
        with self.sessions.begin() as s:
            approval = s.get(Approval, approval_id)
            assert approval is not None
            approvals.decide(
                s,
                approval,
                decision=decision,  # type: ignore[arg-type]
                response=response,
                executor=self.executor,
                events=self.events,
                redactor=self.redactor,
            )

    def close(self) -> None:
        self.engine.dispose()
