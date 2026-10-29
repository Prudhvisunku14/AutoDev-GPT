"""FastAPI application factory."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api import approvals, health, projects, runs
from app.config import Settings, get_settings
from app.core.egress import EgressPolicy
from app.core.events import EventRecorder
from app.core.logging import configure_logging
from app.core.redaction import Redactor
from app.db.session import create_db_engine, create_session_factory, init_db
from app.orchestrator.llm_client import LLMClient, OpenAIResponsesClient, llm_config_problem
from app.orchestrator.loop import Orchestrator
from app.orchestrator.run_manager import RunManager
from app.stacks.catalog import StackCatalog
from app.tools.adapters.workspace_fs import WorkspaceFS
from app.tools.executor import ToolExecutor
from app.tools.gates import register_phase2_tools
from app.tools.policy import PolicyEngine
from app.tools.registry import ToolRegistry

API_PREFIX = "/api/v1"

logger = logging.getLogger("devpilot.http")


def create_app(settings: Settings | None = None, *, llm: LLMClient | None = None) -> FastAPI:
    """Build the app. `llm` replaces the OpenAI client (tests and the offline demo use FakeLLM)."""
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
    )
    run_manager = RunManager(orchestrator)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        logger.info("startup", extra={"version": __version__, "workspaces": str(workspace_fs.root)})
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

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
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
    return app


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
