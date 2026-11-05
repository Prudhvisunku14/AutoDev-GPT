from __future__ import annotations

import io
import logging
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.helpers import API_TOKEN, make_settings, refuse_real_env_file

# Settings fields that the developer's shell could set; tests must not pick them up.
_ENV_VARS = (
    "DEVPILOT_API_TOKEN", "DEVPILOT_HOST", "DEVPILOT_PORT", "DATABASE_URL", "WORKSPACES_DIR", "LOG_LEVEL",
    "OPENAI_API_KEY", "OPENCODE_SERVER_PASSWORD", "N8N_API_KEY", "N8N_WEBHOOK_SECRET", "GITHUB_TOKEN",
    "GOOGLE_OAUTH_CLIENT_SECRET", "DEVPILOT_TOKEN_KEY", "EGRESS_ALLOWLIST_FILE", "GITHUB_OAUTH_CLIENT_ID", "GITHUB_OAUTH_CLIENT_SECRET", "CREDENTIALS_DIR",
    "OPENAI_MODEL", "OPENAI_BASE_URL", "OPENAI_STORE_RESPONSES", "OPENAI_PRICE_INPUT_PER_MTOK",
    "OPENAI_PRICE_OUTPUT_PER_MTOK", "MAX_TURNS", "MAX_TOOL_CALLS", "RUN_BUDGET_USD", "RUN_MAX_TOKENS",
    "DOCKER_CLI", "SANDBOX_MEMORY", "SANDBOX_CPUS", "SANDBOX_PIDS_LIMIT", "SANDBOX_TIMEOUT_SECONDS",
    "SANDBOX_MAX_OUTPUT_BYTES", "OPENCODE_ENABLED", "OPENCODE_BASE_URL", "OPENCODE_SERVER_USERNAME",
    "OPENCODE_MODEL", "OPENCODE_TIMEOUT_SECONDS", "OPENCODE_WORKSPACE_DIR", "OPENCODE_MAX_TOKENS_PER_TASK",
    "OPENCODE_MAX_ATTEMPTS_PER_TASK", "CODING_CHECKPOINTS_DIR", "CODING_WORK_DIR", "MAX_REPAIR_ATTEMPTS",
    "OPENCODE_RUNTIME_IMAGE", "OPENCODE_MANIFEST_FILE", "CODING_LIVE_APPROVED", "GATEWAY_MAX_REQUESTS_PER_TASK",
    "GATEWAY_MAX_OUTPUT_TOKENS", "GATEWAY_MAX_USD_PER_TASK", "SELECTION_POLICY", "SELECTION_WEIGHT_CAPABILITY", "SELECTION_WEIGHT_RELIABILITY",
    "SELECTION_WEIGHT_QUALITY", "SELECTION_WEIGHT_LATENCY", "SELECTION_WEIGHT_COST", "SELECTION_WEIGHT_RESOURCE", "SELECTION_RELIABILITY_ALPHA",
    "SELECTION_RELIABILITY_BETA", "SELECTION_LATENCY_REFERENCE_SECONDS", "SELECTION_COST_REFERENCE_USD", "SELECTION_MEMORY_REFERENCE_MB",
    "RECOVERY_POLICY", "RECOVERY_REPEAT_LIMIT", "RECOVERY_MIN_REPAIR_RATE", "RECOVERY_MIN_REPAIR_SAMPLES", "RECOVERY_MAX_ATTEMPT_TIMEOUT_SECONDS",
    "REPOSITORY_SOURCE", "REPOSITORY_SEARCH_LIMIT", "REPOSITORY_REUSE_THRESHOLD", "REPOSITORY_MAX_ARCHIVE_MB", "REPOSITORY_MAX_FILES",
)


_LIVE_FLAGS = {
    "live_openai": "RUN_LIVE_OPENAI_TESTS",
    "live_opencode": "RUN_LIVE_OPENCODE_TESTS",
    "live_n8n": "RUN_LIVE_N8N_TESTS",
    "live_google": "RUN_LIVE_GOOGLE_TESTS",
    "live_github": "RUN_LIVE_GITHUB_TESTS",
    "docker": "RUN_DOCKER_TESTS",
}

if os.environ.get(_LIVE_FLAGS["live_openai"], "").lower() != "true":  # only an approved live run reads the real key
    sys.addaudithook(refuse_real_env_file)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Opt-in tests run only when their flag is exactly `true` in the process environment."""
    for item in items:
        for marker, flag in _LIVE_FLAGS.items():
            if marker in item.keywords and os.environ.get(flag, "").lower() != "true":
                item.add_marker(pytest.mark.skip(reason=f"opt-in: set {flag}=true (needs approval; may cost money)"))


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {API_TOKEN}"}


@pytest.fixture
def log_buffer(app: FastAPI) -> io.StringIO:
    """Captures exactly what the app's redacting JSON log handler writes."""
    buffer = io.StringIO()
    handler = next(h for h in logging.getLogger().handlers if h.get_name() == "devpilot-json")
    assert isinstance(handler, logging.StreamHandler)
    handler.setStream(buffer)
    return buffer
