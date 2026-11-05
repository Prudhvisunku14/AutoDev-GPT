"""Opt-in live test against a real, local n8n (scripts/n8n-local.ps1). Needs approval; contacts only 127.0.0.1:5678.

Skipped unless RUN_LIVE_N8N_TESTS=true. It reads N8N_WEBHOOK_SECRET and N8N_API_KEY from the *process environment* (never
from .env), checks that every registered workflow is imported and active, triggers the `notify` and `run-events` workflows
with DevPilot's real signed requests, and waits for n8n to record a successful execution of each.

    $env:RUN_LIVE_N8N_TESTS = 'true'; $env:N8N_WEBHOOK_SECRET = '<same as n8n>'; $env:N8N_API_KEY = '<from n8n>'
    cd backend; & $env:DEVPILOT_PYTHON -m pytest tests/live/test_n8n_live.py -s
"""

from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path

import pytest

from app.core.egress import EgressPolicy
from app.n8n.runtime import build_n8n_runtime
from app.n8n.signing import signed_headers
from tests.helpers import ALLOWLIST_FILE, make_settings

pytestmark = pytest.mark.live_n8n


def _runtime(tmp_path: Path):  # noqa: ANN202
    secret, key = os.environ.get("N8N_WEBHOOK_SECRET"), os.environ.get("N8N_API_KEY")
    if not secret or not key:
        pytest.skip("set N8N_WEBHOOK_SECRET and N8N_API_KEY in this terminal (the values n8n uses)")
    settings = make_settings(tmp_path, n8n_enabled=True, n8n_webhook_secret=secret, n8n_api_key=key, n8n_callback_url=None)
    runtime = build_n8n_runtime(settings, EgressPolicy.from_file(ALLOWLIST_FILE))
    assert runtime is not None and not runtime.client.simulated
    return runtime, secret


def test_every_registered_workflow_is_imported_and_active(tmp_path: Path) -> None:
    runtime, _ = _runtime(tmp_path)
    report = runtime.health()
    assert report["reachable"], report["error"]
    missing = [w["key"] for w in report["workflows"] if not (w["found"] and w["active"])]
    assert not missing, f"import and activate these workflows in n8n: {missing}"


@pytest.mark.parametrize(("key", "inputs"), [("notify", {"title": "DevPilot live check", "message": "a signed trigger"}),
                                             ("run-events", {"event": "live-check", "summary": "a signed run event"})])  # fmt: skip
def test_a_signed_trigger_runs_to_a_successful_execution(tmp_path: Path, key: str, inputs: dict[str, str]) -> None:
    runtime, secret = _runtime(tmp_path)
    spec = runtime.catalog.get(key)
    assert spec is not None
    workflow = next(w for w in runtime.client.workflows() if spec.path in w.webhook_paths)
    before = {e.id for e in runtime.client.executions(workflow.id, limit=20)}
    body = json.dumps({"trigger_id": f"trg-{secrets.token_hex(8)}", "workflow": key, "project_id": "0" * 32, "run_id": "0" * 32, "inputs": inputs, "callback_url": None},
                      sort_keys=True, separators=(",", ":")).encode("utf-8")  # fmt: skip
    answer = runtime.client.trigger(spec.path, body, signed_headers(secret, body))
    assert 200 <= answer.status_code < 300
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        new = [e for e in runtime.client.executions(workflow.id, limit=20) if e.id not in before]
        if new and new[0].status in ("success", "error", "crashed"):
            assert new[0].status == "success", f"n8n recorded {new[0].status} for {key}"
            return
        time.sleep(1)
    pytest.fail(f"n8n recorded no finished execution of {key} within 30 seconds")
