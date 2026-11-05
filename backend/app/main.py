"""FastAPI application factory."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api import approvals, artifacts, budget, capabilities, github, health, history, integrations, knowledge, n8n, projects, resources, runs, session, tools
from app.coding.attempt_store import SqlAttemptStore
from app.coding.candidate import CandidateWorkspaces
from app.config import Settings, get_settings
from app.core.egress import EgressPolicy
from app.core.events import EventRecorder
from app.core.logging import configure_logging
from app.core.redaction import Redactor
from app.core.secret_store import EncryptedFileStore
from app.core.sessions import CSRF_HEADER, SessionStore
from app.db.session import create_db_engine, create_session_factory, init_db
from app.orchestrator.fake_llm import FakeLLM
from app.orchestrator.llm_client import LLMClient, OpenAIResponsesClient, llm_config_problem
from app.orchestrator.loop import Orchestrator
from app.orchestrator.run_manager import RunManager
from app.orchestrator.start_policy import StartPolicy
from app.orchestrator.state_machine import FULL_RUN_STATES, ORCHESTRATED_STATES
from app.drive.oauth import build_cipher
from app.n8n.runtime import N8nRuntime
from app.repos.github_oauth import GitHubConnection
from app.repos.source import RepositorySource
from app.sandbox.docker_cli import SandboxError
from app.sandbox.runner import SandboxRunner
from app.selection.selector import Selector
from app.stacks.catalog import StackCatalog
from app.toolcatalog.catalog import ToolCatalog
from app.toolcatalog.model import load_facts
from app.tools.adapters.catalog import register_catalog_tools
from app.tools.adapters.coding import CodingRuntime, recover_attempts, register_coding_tools
from app.tools.adapters.drive import DriveRuntime, register_drive_tools
from app.tools.adapters.gap import register_gap_tool
from app.tools.adapters.github_publish import PublishRuntime, register_publish_tool
from app.tools.adapters.n8n import register_n8n_tools
from app.tools.adapters.repositories import RepositoryRuntime, register_repository_tools
from app.tools.adapters.sandbox import register_sandbox_tools
from app.tools.adapters.workspace_fs import WorkspaceFS
from app.tools.executor import ToolExecutor
from app.tools.gates import register_phase2_tools
from app.tools.policy import PolicyEngine
from app.tools.registry import ToolRegistry
from app.verification.web import WebVerifier

API_PREFIX = "/api/v1"
UI_DIR = Path(__file__).resolve().parent / "ui"  # the read-only dashboard: static files that call the API with the user's token

logger = logging.getLogger("devpilot.http")


def create_app(
    settings: Settings | None = None,
    *,
    llm: LLMClient | None = None,
    sandbox: SandboxRunner | None = None,
    coding: Selector | None = None,
    repositories: RepositorySource | None = None,
    drive: DriveRuntime | None = None,
    n8n_runtime: N8nRuntime | None = None,
    gpt_label: str | None = None,
    start_policy: StartPolicy | None = None,
    web_verifier: WebVerifier | None = None,
    github_connection: GitHubConnection | None = None,
    github_http: Any = None,
) -> FastAPI:
    """Build the app. `llm` replaces the OpenAI client (tests and the offline demo use FakeLLM). `gpt_label` names
    what `llm` really is in reports, for a real client wrapped in a guard (app/plan_cli.py). `start_policy` may refuse
    a start across runs, such as a session-wide spending cap (scripts/live_server.py).

    `sandbox` enables the Docker sandbox tool. Without it no container is ever started, so
    tests never touch Docker. `coding`, which needs a sandbox, chooses who writes the code and lets
    GPT drive a run to the end. Without it a run pauses at IMPLEMENTING. `repositories` enables the
    approval-gated search and import of public code in PLANNING (fixtures offline; GitHub once approved).
    `drive` enables the Google Drive and RAG knowledge-base tools (a fixture Drive offline; off by default).
    `n8n_runtime` enables `trigger_workflow`, `get_workflow_result` and the n8n routes (a simulated n8n offline; off by default).
    `github_connection` is the person's GitHub connection (built from settings when None); once it is configured and a coding
    runtime exists, `publish_repository_changes` is offered. `github_http` replaces the HTTP client GitHub reads use (tests).
    """
    settings = settings or get_settings()
    redactor = Redactor(settings.secret_values())
    configure_logging(settings.log_level, redactor)

    # Fail fast on bad configuration: these raise before the server accepts requests.
    egress = EgressPolicy.from_file(settings.egress_allowlist_file)
    catalog = StackCatalog.load()
    workspace_fs = WorkspaceFS(
        settings.workspaces_dir,
        max_file_bytes=settings.workspace_max_file_bytes,
        max_files=settings.workspace_max_files,
    )
    engine = create_db_engine(settings)
    init_db(engine)
    session_factory = create_session_factory(engine)
    events = EventRecorder(redactor)

    registry = ToolRegistry()
    register_phase2_tools(registry)
    if sandbox is not None:
        register_sandbox_tools(registry)
    gpt_label = gpt_label or ("FakeLLM (scripted)" if llm is not None else "OpenAI Responses API")
    runtime: CodingRuntime | None = None
    candidates: WorkspaceFS | None = None
    if coding is not None:
        if sandbox is None:
            raise ValueError("a coding provider needs the sandbox: nothing could verify its work")
        candidates = WorkspaceFS(settings.coding_work_dir, max_file_bytes=settings.workspace_max_file_bytes, max_files=settings.workspace_max_files)
        web = web_verifier or WebVerifier(egress, docker=settings.docker_cli, work_dir=settings.coding_work_dir / "web-verification",
                                         memory=settings.sandbox_memory, cpus=settings.sandbox_cpus, pids=settings.sandbox_pids_limit,
                                         timeout=settings.sandbox_timeout_seconds, capture_bytes=settings.sandbox_max_output_bytes)
        runtime = CodingRuntime(selector=coding, store=SqlAttemptStore(session_factory, redactor), candidates=CandidateWorkspaces(candidates), sandbox=sandbox, gpt_label=gpt_label, web=web)
        register_coding_tools(registry, runtime)
    if repositories is not None:
        register_repository_tools(registry, RepositoryRuntime(repositories))
    elif settings.repository_source == "github":  # a repository the person names is read, so a run may start from existing code
        register_gap_tool(registry)
    if drive is not None:
        register_drive_tools(registry, drive)
    if n8n_runtime is not None:
        register_n8n_tools(registry, n8n_runtime)
    if github_connection is None:
        cipher, cipher_problem = build_cipher(settings.devpilot_token_key)
        github_connection = GitHubConnection(egress, EncryptedFileStore(settings.credentials_dir, cipher, problem=cipher_problem),
                                             client_id=settings.github_oauth_client_id, client_secret=settings.github_oauth_client_secret,
                                             redirect_uri=settings.github_oauth_redirect_uri, scopes=tuple(settings.github_oauth_scopes.split()))  # fmt: skip
    if runtime is not None and github_connection.blocker() is None:
        register_publish_tool(registry, PublishRuntime(runtime.candidates, github_connection, egress, redactor, client=github_http))
    tool_catalog = ToolCatalog(load_facts(settings.tool_catalog_file))  # a malformed catalog stops the app here
    register_catalog_tools(registry, tool_catalog)
    executor = ToolExecutor(registry, PolicyEngine(), redactor, max_output_chars=settings.tool_output_max_chars)
    llm_provider, llm_problem = _llm_wiring(settings, egress, llm)
    orchestrator = Orchestrator(
        settings=settings,
        session_factory=session_factory,
        registry=registry,
        executor=executor,
        events=events,
        catalog=catalog,
        redactor=redactor,
        llm_provider=llm_provider,
        sandbox=sandbox,
        workspace=workspace_fs,
        candidates=candidates,
        orchestrated_states=FULL_RUN_STATES if runtime is not None else ORCHESTRATED_STATES,
    )
    run_manager = RunManager(orchestrator)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        logger.info("startup", extra={"version": __version__, "workspaces": str(workspace_fs.root)})
        if sandbox is not None:
            _sweep_sandbox(sandbox)
        if runtime is not None:  # before any run can advance and report an interrupted call to GPT
            recover_attempts(runtime, settings, session_factory)
        await run_manager.start()
        yield
        await run_manager.stop()
        engine.dispose()

    # Interactive docs are off: every route except /health requires the bearer token.
    app = FastAPI(
        title="DevPilot AI",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.redactor = redactor
    app.state.egress = egress
    app.state.workspace_fs = workspace_fs
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.events = events
    app.state.catalog = catalog
    app.state.registry = registry
    app.state.executor = executor
    app.state.orchestrator = orchestrator
    app.state.run_manager = run_manager
    app.state.llm_problem = llm_problem
    app.state.llm_provider = llm_provider  # the same GPT client and limits for grounded answers (api/knowledge.py)
    app.state.sandbox = sandbox
    app.state.drive = drive
    app.state.n8n = n8n_runtime
    app.state.gpt_label = gpt_label
    app.state.scripted_llm = isinstance(llm, FakeLLM)  # scripted GPT spends nothing; any other client may, so its starts need approval
    app.state.coding_selector = coding if runtime is not None else None
    app.state.repositories = repositories
    app.state.github = github_connection
    app.state.github_http = github_http
    app.state.web_sessions = SessionStore(session_factory)  # the dashboard's HttpOnly session cookies (app/core/sessions.py)
    app.state.tool_catalog = tool_catalog
    tool_catalog.attach(app.state)
    app.state.start_policy = start_policy

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type", CSRF_HEADER],
        allow_credentials=True,  # the session cookie, from the configured dashboard origins only
    )

    @app.middleware("http")
    async def log_requests(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        request_id = uuid.uuid4().hex[:12]
        started = time.perf_counter()
        response = await call_next(request)
        # Path only: query strings and headers can carry secrets.
        logger.info(
            "request",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            },
        )
        response.headers["X-Request-ID"] = request_id
        return response

    app.include_router(health.public_router, prefix=API_PREFIX)
    app.include_router(health.router, prefix=API_PREFIX)
    app.include_router(projects.router, prefix=API_PREFIX)
    app.include_router(runs.router, prefix=API_PREFIX)
    app.include_router(approvals.router, prefix=API_PREFIX)
    app.include_router(artifacts.router, prefix=API_PREFIX)
    app.include_router(budget.router, prefix=API_PREFIX)
    app.include_router(capabilities.router, prefix=API_PREFIX)
    app.include_router(history.router, prefix=API_PREFIX)
    app.include_router(tools.router, prefix=API_PREFIX)
    app.include_router(session.router, prefix=API_PREFIX)
    app.include_router(knowledge.router, prefix=API_PREFIX)
    app.include_router(resources.router, prefix=API_PREFIX)
    app.include_router(github.router)  # its own paths: GitHub's OAuth callback is not under the API prefix
    app.include_router(integrations.router)  # its own paths: the OAuth callback is not under the API prefix
    app.include_router(n8n.router)  # its own paths: n8n's signed callback is not under the API prefix
    if UI_DIR.is_dir():
        app.mount("/ui", StaticFiles(directory=UI_DIR, html=True), name="ui")
    return app


def _sweep_sandbox(sandbox: SandboxRunner) -> None:
    """Containers left by a crashed earlier process are removed before new work starts."""
    try:
        removed = sandbox.sweep_orphans()
    except SandboxError as exc:
        logger.warning("sandbox sweep skipped: %s", exc)
        return
    if removed:
        logger.warning("removed %d sandbox container(s) left by an earlier process", removed)


def _llm_wiring(
    settings: Settings, egress: EgressPolicy, llm: LLMClient | None
) -> tuple[Callable[[], LLMClient], Callable[[], str | None]]:
    """The OpenAI client is built on first use, so a missing key never blocks startup."""
    if llm is not None:
        return (lambda: llm), (lambda: None)

    cache: list[LLMClient] = []

    def provider() -> LLMClient:
        if not cache:
            cache.append(OpenAIResponsesClient(settings))
        return cache[0]

    return provider, (lambda: llm_config_problem(settings, egress))
