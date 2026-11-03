"""The shared backend engine, assembled for one terminal build.

This is the same engine the API and dashboard serve: `create_app` with the real GPT orchestrator, the OpenCode launcher (behind its
API filter and model gateway), the Docker sandbox, the run limits, the verification engine and the delivery gates. Nothing here
generates code. What this module adds is the sequence the verified builds used: the key and model checks, a dry launch of the real
containers, the keep-awake request and the guard process that stops the containers if this process ends, then the app itself.

Settings come only from the user's own configuration and the process environment, never from the repository's `.env`, and the limits
are the ones the verified builds ran under.
"""

from __future__ import annotations

import secrets
import shutil
import warnings
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import openai

from app.cli.paths import Paths
from app.cli.userconfig import UserConfig
from app.config import Settings

# What the verified builds ran under (scripts/live_build.py), except the per-build cap, which the user approves.
LIMITS: dict[str, Any] = {
    "openai_max_output_tokens": 3_000, "openai_max_retries": 0, "run_max_tokens": 300_000, "max_turns": 12, "max_tool_calls": 12,
    "run_wall_clock_seconds": 1_200.0, "opencode_max_tokens_per_task": 200_000, "opencode_max_attempts_per_task": 2,
    "opencode_timeout_seconds": 300.0, "max_repair_attempts": 1, "gateway_max_requests_per_task": 15, "gateway_max_output_tokens": 4_000,
}  # fmt: skip
GUARD_MARGIN_SECONDS = 300.0
CAPABILITIES = frozenset({"code_generation", "test_generation", "documentation"})
API = "/api/v1"


def build_settings(
    paths: Paths,
    config: UserConfig,
    *,
    key: str | None,
    image_id: str | None,
    manifest: Path | None,
    cap_usd: float,
    live_approved: bool,
) -> Settings:
    """Settings for this installation. `live_approved` is True only after the person typed their spending approval."""
    model = config.model or ""
    return Settings(
        _env_file=None,  # never the repository's .env
        devpilot_api_token=secrets.token_urlsafe(32),  # throwaway: nothing outside this process ever calls this app
        database_url=f"sqlite:///{paths.database.as_posix()}", workspaces_dir=paths.workspaces, coding_checkpoints_dir=paths.checkpoints,
        coding_work_dir=paths.work, history_dir=None, log_level="WARNING",
        openai_api_key=key, openai_model=model or None, opencode_model=f"openai/{model}" if model else None,
        openai_price_input_per_mtok=config.price_input_per_mtok, openai_price_output_per_mtok=config.price_output_per_mtok,
        run_budget_usd=cap_usd, gateway_max_usd_per_task=round(min(0.30, cap_usd * 0.6), 4),
        opencode_enabled=True, opencode_runtime_image=image_id, opencode_manifest_file=manifest,
        coding_live_approved=live_approved, require_spend_approval=True, **LIMITS,
    )  # fmt: skip


def model_access(key: str, model: str, *, client_factory: Callable[..., Any] = openai.OpenAI) -> str | None:
    """None when the key can see the model (GET /v1/models/{model}: not billed, and no quota is proven); else why not.
    The key is sent to OpenAI only, and never appears in the answer."""
    try:
        client_factory(api_key=key, max_retries=0, timeout=30.0).models.retrieve(model)
    except openai.AuthenticationError:
        return "OpenAI did not accept the API key"
    except (openai.NotFoundError, openai.PermissionDeniedError):
        return f"the key cannot see the model {model}: check the name, or your account's access"
    except openai.APIError as exc:
        return f"OpenAI could not be reached ({type(exc).__name__}): check your internet connection"
    return None


@dataclass
class Session:
    """What a build drives: a client onto the in-process app, and the app itself (for its workspace and redactor)."""

    client: Any
    headers: dict[str, str]
    app: Any
    settings: Settings


class LiveEngine(AbstractContextManager[Session]):
    """The real engine. Entering it checks the key and model, dry-launches the real containers, starts the guard, and builds the app."""

    def __init__(self, paths: Paths, config: UserConfig, *, key: str, image_id: str, manifest: Path, cap_usd: float, say: Callable[[str], None] = print) -> None:
        self._args = (paths, config, key, image_id, manifest, cap_usd)
        self._say = say
        self._client: Any = None

    def __enter__(self) -> Session:
        # Imported here: these pull in the whole engine, which `devpilot doctor` and `--help` do not need.
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message=".*httpx.*")
            from fastapi.testclient import TestClient

        from app.coding.guard_process import keep_awake, start_guard
        from app.coding.launcher import NETWORKS, LaunchRefused, LaunchRequest, OpenCodeLauncher
        from app.core.egress import EgressPolicy
        from app.main import create_app
        from app.orchestrator.start_policy import SessionBudget
        from app.sandbox.docker_cli import DockerCli
        from app.sandbox.process import SubprocessRunner
        from app.sandbox.runner import DockerSandbox
        from app.selection.selector import ProviderOption, ProviderSelector
        import time

        paths, config, key, image_id, manifest, cap = self._args
        settings = build_settings(paths, config, key=key, image_id=image_id, manifest=manifest, cap_usd=cap, live_approved=True)
        paths.ensure()
        cli = DockerCli(settings.docker_cli, SubprocessRunner())

        self._say("Keeping the computer awake and starting the container guard…")
        keep_awake()
        start_guard(paths.guard / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"), deadline=settings.run_wall_clock_seconds + GUARD_MARGIN_SECONDS,
                    networks=[name for name, _ in reversed(NETWORKS)], docker=settings.docker_cli)  # fmt: skip

        self._say("Checking that your OpenAI key can see the model (free, no tokens)…")
        problem = model_access(key, config.model or "")
        if problem:
            raise EngineRefused(problem + ". Nothing was billed.")

        launcher = OpenCodeLauncher(settings, cli, EgressPolicy.from_file(settings.egress_allowlist_file))
        problem = launcher.unavailable_reason()
        if problem:
            raise EngineRefused(f"the coding engine is not ready: {problem}")
        self._say("Test-starting the real containers, then stopping them (still free)…")
        workspace = settings.coding_work_dir / "dry-launch"
        shutil.rmtree(workspace, ignore_errors=True)
        workspace.mkdir(parents=True)
        try:
            session = launcher.start(LaunchRequest(run_id="dry-launch", call_id="dry-launch", workspace=workspace, token_budget=1_000, usd_budget=0.01))
        except LaunchRefused as exc:
            raise EngineRefused(f"the containers could not start: {exc}") from exc
        finally:
            shutil.rmtree(workspace, ignore_errors=True)
        session.close()
        stopped = session.writer.terminate()
        if not stopped.confirmed:
            raise EngineRefused(f"the test containers could not be confirmed stopped: {stopped.evidence}")

        selector = ProviderSelector([ProviderOption("opencode_server", launcher, CAPABILITIES, simulated=False)])
        budget = SessionBudget(cap, cap, run_seconds=settings.run_wall_clock_seconds + GUARD_MARGIN_SECONDS, ends_at=time.monotonic() + settings.run_wall_clock_seconds + 2 * GUARD_MARGIN_SECONDS)
        app = create_app(settings, sandbox=DockerSandbox.from_settings(settings), coding=selector, start_policy=budget)
        self._client = TestClient(app)
        self._client.__enter__()  # starts the app's lifespan: the run manager, and recovery of any attempt an earlier process left
        return Session(self._client, {"Authorization": f"Bearer {settings.devpilot_api_token.get_secret_value()}"}, app, settings)

    def __exit__(self, *exc: object) -> None:
        if self._client is not None:
            self._client.__exit__(*exc)  # type: ignore[arg-type]


class EngineRefused(Exception):
    """The engine will not start, before anything was spent. The message says why."""
