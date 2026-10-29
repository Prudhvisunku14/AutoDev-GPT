"""Secrets never reach the DB or logs (SECURITY T6), and bad config stops startup."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.egress import EgressConfigError
from app.main import create_app
from tests.helpers import API_TOKEN, FAKE_GITHUB_TOKEN, FAKE_OPENAI_KEY, make_settings

SECRETS = (API_TOKEN, FAKE_OPENAI_KEY, FAKE_GITHUB_TOKEN)


def test_secrets_never_reach_the_database_or_logs(
    client: TestClient, app: FastAPI, auth: dict[str, str], log_buffer: io.StringIO, tmp_path: Path
) -> None:
    pasted = f"Use key {FAKE_OPENAI_KEY} and token {FAKE_GITHUB_TOKEN}. Also {API_TOKEN}."
    project = client.post(
        f"/api/v1/projects", json={"name": f"leak {FAKE_OPENAI_KEY}", "requirement_text": pasted}, headers=auth
    ).json()
    assert FAKE_OPENAI_KEY not in project["requirement_text"]
    assert "[REDACTED]" in project["requirement_text"]
    client.post(f"/api/v1/projects/{project['id']}/runs", headers=auth)
    client.get("/api/v1/status", headers=auth)
    client.get(f"/api/v1/projects?token={API_TOKEN}", headers=auth)  # secret in a query string

    app.state.engine.dispose()  # flush the WAL so the main file is complete
    stored = b"".join(p.read_bytes() for p in (tmp_path / "data").iterdir())
    logs = log_buffer.getvalue()
    for secret in SECRETS:
        assert secret.encode() not in stored, "secret found in the database files"
        assert secret not in logs, "secret found in the logs"

    # The logs are structured and include the request lines.
    entries = [json.loads(line) for line in logs.splitlines()]
    assert any(e["msg"] == "request" and e["extra"]["status"] == 201 for e in entries)
    assert any(e["msg"].startswith("event project.created") for e in entries)


def test_startup_fails_without_a_valid_allowlist(tmp_path: Path) -> None:
    with pytest.raises(EgressConfigError):
        create_app(make_settings(tmp_path, egress_allowlist_file=tmp_path / "missing.yaml"))


def test_app_starts_and_stops_cleanly(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as c:
        assert c.get("/api/v1/health").status_code == 200
    assert (tmp_path / "workspaces").is_dir()
    assert (tmp_path / "data" / "test.db").is_file()


def test_status_degrades_when_workspaces_are_not_writable(
    client: TestClient, app: FastAPI, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import tempfile

    def broken(*_args: object, **_kwargs: object) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(tempfile, "TemporaryFile", broken)
    body = client.get("/api/v1/status", headers=auth).json()
    assert body["status"] == "degraded"
    assert body["workspaces_writable"] is False
