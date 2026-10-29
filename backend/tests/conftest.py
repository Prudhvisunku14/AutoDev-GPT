from __future__ import annotations

import io
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.helpers import API_TOKEN, make_settings

# Settings fields that the developer's shell could set; tests must not pick them up.
_ENV_VARS = (
    "DEVPILOT_API_TOKEN", "DEVPILOT_HOST", "DEVPILOT_PORT", "DATABASE_URL", "WORKSPACES_DIR", "LOG_LEVEL",
    "OPENAI_API_KEY", "OPENCODE_SERVER_PASSWORD", "N8N_API_KEY", "N8N_WEBHOOK_SECRET", "GITHUB_TOKEN",
    "GOOGLE_OAUTH_CLIENT_SECRET", "DEVPILOT_TOKEN_KEY", "EGRESS_ALLOWLIST_FILE",
)


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
