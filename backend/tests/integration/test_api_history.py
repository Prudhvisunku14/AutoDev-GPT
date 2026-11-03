"""Earlier terminal builds, shown read-only from summary.json and the delivered files; and what /capabilities says is missing."""

from __future__ import annotations

import io
import json
import os
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import API_PREFIX, create_app
from app.orchestrator.fake_llm import FakeLLM
from tests.helpers import API_TOKEN, FAKE_OPENAI_KEY, make_settings

HEADERS = {"Authorization": f"Bearer {API_TOKEN}"}
PASSED, FAILED, EMPTY = "20261103T160731Z", "20261103T073422Z", "20261103T071030Z"
PROJECT = "6cec87548be14136b3dfff171fe58ee9"
_OPENED_DATABASES: list[str] = []
_WATCHING: list[bool] = []


def _audit(event: str, args: tuple[Any, ...]) -> None:
    if _WATCHING and event in ("open", "sqlite3.connect") and args and isinstance(args[0], str | bytes | os.PathLike):
        name = os.fsdecode(args[0])
        if name.endswith(("demo.db", "demo.db-wal", "demo.db-shm")):
            _OPENED_DATABASES.append(name)


sys.addaudithook(_audit)


def _report(state: str, verdict: str, delivered: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "project": {"id": PROJECT, "name": "Student attendance tracker", "requirement": "Create a Python student attendance tracker.", "stack": None},
        "run": {"id": "be65d86a9550448085cfbc519e541dfc", "state": state, "outcome": state.lower(), "detail": None, "turns": 6, "tokens": 9000, "cost_usd": 0.131854, "repairs": 0},
        "verifications": [{"verdict": "PASSED", "passed": 16, "ran": 16}],
        "acceptance": {"configured": True, "verdict": verdict, "passed": 8, "total": 8},
        "delivered_files": delivered,
        "provenance": {"gpt": "OpenAI Responses API", "coding_providers": ["opencode_server"], "sandbox": "DockerSandbox", "simulated": [], "fully_real": True},
    }


@pytest.fixture
def history(tmp_path: Path) -> Path:
    root = tmp_path / "live-demo"
    passed = root / PASSED
    files = passed / "workspaces" / PROJECT
    (files / "__pycache__").mkdir(parents=True)
    (files / "attendance.py").write_text("class AttendanceTracker:\n    pass\n", encoding="utf-8")
    (files / "test_attendance.py").write_text("import unittest\n", encoding="utf-8")
    (files / ".env").write_text("OPENAI_API_KEY=placeholder\n", encoding="utf-8")
    (files / "__pycache__" / "attendance.cpython-312.pyc").write_bytes(b"\x00compiled")
    (passed / "demo.db").write_bytes(b"SQLite format 3\x00 never to be opened")
    delivered = [{"path": "attendance.py", "change": "added", "bytes": 33}, {"path": "test_attendance.py", "change": "added", "bytes": 16}]
    summary = {"state": "COMPLETED", "outcome": "completed", "detail": "Delivered attendance.py", "turns": 6, "cost_usd": 0.131854, "tokens_in": 7000, "tokens_out": 2000,
               "seconds": 156.1, "delivered": ["attendance.py", "test_attendance.py"], "report": _report("COMPLETED", "PASSED", delivered)}  # fmt: skip
    (passed / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    failed = root / FAILED
    failed.mkdir()
    (failed / "demo.db").write_bytes(b"SQLite format 3\x00")
    (failed / "summary.json").write_text(json.dumps({"state": "FAILED", "outcome": "coding_failed", "cost_usd": 0.379808, "delivered": [], "report": _report("FAILED", "NOT_RUN", [])}), encoding="utf-8")
    (root / EMPTY).mkdir()
    return root


def _tree(root: Path) -> dict[str, tuple[int, int]]:
    return {str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns) for p in sorted(root.rglob("*"))}


@pytest.fixture
def client(tmp_path: Path, history: Path) -> Any:
    settings = make_settings(tmp_path, history_dir=history)
    with TestClient(create_app(settings, llm=FakeLLM())) as test_client:
        yield test_client


def test_history_lists_terminal_builds_newest_first_and_says_they_are_historical(client: TestClient) -> None:
    records = client.get(f"{API_PREFIX}/history/runs", headers=HEADERS).json()
    assert [r["id"] for r in records] == [PASSED, FAILED]  # the folder without a summary is not shown
    assert records[0]["state"] == "COMPLETED" and records[0]["cost_usd"] == 0.131854 and records[0]["tokens"] == 9000
    assert records[0]["report"] is None and "terminal build" in records[0]["source"]
    detail = client.get(f"{API_PREFIX}/history/runs/{PASSED}", headers=HEADERS).json()
    assert detail["report"]["acceptance"]["verdict"] == "PASSED"


def test_history_files_are_read_inside_the_runs_own_project_folder(client: TestClient) -> None:
    files = client.get(f"{API_PREFIX}/history/runs/{PASSED}/files", headers=HEADERS).json()["files"]
    assert "attendance.py" in files
    content = client.get(f"{API_PREFIX}/history/runs/{PASSED}/files/content", params={"path": "attendance.py"}, headers=HEADERS)
    assert content.status_code == 200 and "AttendanceTracker" in content.json()["content"]
    escape = client.get(f"{API_PREFIX}/history/runs/{PASSED}/files/content", params={"path": "../../summary.json"}, headers=HEADERS)
    assert escape.status_code == 400


def test_a_verified_historical_build_downloads_without_secrets_or_caches(client: TestClient) -> None:
    described = client.get(f"{API_PREFIX}/history/runs/{PASSED}/artifact", headers=HEADERS).json()
    assert described["available"] is True and described["filename"].endswith(".zip")
    assert {f["path"] for f in described["files"]} == {"attendance.py", "test_attendance.py"}
    assert {e["path"] for e in described["excluded"]} == {".env", "__pycache__/attendance.cpython-312.pyc"}
    answer = client.get(f"{API_PREFIX}/history/runs/{PASSED}/artifact.zip", headers=HEADERS)
    assert answer.status_code == 200 and answer.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(answer.content)).namelist()
    assert any(n.endswith("/attendance.py") for n in names) and any(n.endswith("devpilot-verification/report.md") for n in names)
    assert not any(n.endswith(".env") or n.endswith(".pyc") for n in names)
    manifest = next(n for n in names if n.endswith("MANIFEST.txt"))
    assert "Historical" in zipfile.ZipFile(io.BytesIO(answer.content)).read(manifest).decode()


def test_a_failed_historical_build_has_nothing_to_download(client: TestClient) -> None:
    described = client.get(f"{API_PREFIX}/history/runs/{FAILED}/artifact", headers=HEADERS).json()
    assert described["available"] is False and "FAILED" in described["reason"]
    assert client.get(f"{API_PREFIX}/history/runs/{FAILED}/artifact.zip", headers=HEADERS).status_code == 409
    assert client.get(f"{API_PREFIX}/history/runs/{FAILED}/files", headers=HEADERS).json() == {"files": []}


@pytest.mark.parametrize("record_id", ["20261103T16073Z", "nope", "20261103T160731Z.", EMPTY])
def test_unknown_or_malformed_records_are_not_found(client: TestClient, record_id: str) -> None:
    assert client.get(f"{API_PREFIX}/history/runs/{record_id}", headers=HEADERS).status_code == 404


def test_history_needs_the_token(client: TestClient) -> None:
    for path in ("/history/runs", f"/history/runs/{PASSED}", f"/history/runs/{PASSED}/artifact.zip"):
        assert client.get(f"{API_PREFIX}{path}").status_code == 401


def test_reading_history_writes_nothing_and_never_opens_a_database(client: TestClient, history: Path) -> None:
    before = _tree(history)
    _OPENED_DATABASES.clear()
    _WATCHING.append(True)
    try:
        for record in (PASSED, FAILED):
            for path in ("", "/files", "/artifact", "/artifact.zip"):
                client.get(f"{API_PREFIX}/history/runs/{record}{path}", headers=HEADERS)
        client.get(f"{API_PREFIX}/history/runs", headers=HEADERS)
    finally:
        _WATCHING.clear()
    assert _OPENED_DATABASES == []
    assert _tree(history) == before  # no workspace was created for the failed run, nothing was touched


def test_without_a_history_folder_there_is_no_history(tmp_path: Path) -> None:
    with TestClient(create_app(make_settings(tmp_path, history_dir=tmp_path / "missing"), llm=FakeLLM())) as client:
        assert client.get(f"{API_PREFIX}/history/runs", headers=HEADERS).json() == []
        assert client.get(f"{API_PREFIX}/capabilities", headers=HEADERS).json()["history"] is False
    assert not (tmp_path / "missing").exists()


def test_capabilities_name_every_setting_the_coding_engine_still_needs(tmp_path: Path) -> None:
    with TestClient(create_app(make_settings(tmp_path), llm=FakeLLM())) as client:
        coding = client.get(f"{API_PREFIX}/capabilities", headers=HEADERS).json()["coding"]
    assert coding["ready"] is False
    for name in ("OPENCODE_ENABLED is false", "CODING_LIVE_APPROVED is false", "OPENCODE_MANIFEST_FILE is not set", "OPENCODE_MODEL", "OPENAI_PRICE_INPUT_PER_MTOK", "OPENAI_MAX_RETRIES must be 0"):
        assert name in coding["detail"]
    assert "OPENAI_API_KEY is not set" not in coding["detail"]  # the fake key is configured: its presence counts, never its value
    assert FAKE_OPENAI_KEY not in coding["detail"]
