"""Every entry point wires GitHub, Google Drive and n8n the same way: exactly when their own settings enable them."""

from __future__ import annotations

import re
from pathlib import Path

from app.config import REPO_ROOT
from app.integrations import integrations_for
from app.main import create_app
from app.orchestrator.fake_llm import FakeLLM
from tests.helpers import make_settings

ENTRY_POINTS = [REPO_ROOT / "backend" / "app" / "__main__.py", REPO_ROOT / "backend" / "app" / "cli" / "engine.py", REPO_ROOT / "backend" / "scripts" / "live_server.py"]
INTEGRATION_TOOLS = {"search_repositories", "import_repository", "trigger_workflow", "get_workflow_result", "request_drive_authorization"}


def test_nothing_is_wired_while_every_setting_is_off(tmp_path: Path) -> None:
    wired = integrations_for(make_settings(tmp_path), None)
    assert wired == {"repositories": None, "drive": None, "n8n_runtime": None}
    app = create_app(make_settings(tmp_path), llm=FakeLLM(), **wired)
    try:
        assert not INTEGRATION_TOOLS & set(app.state.registry.names)
    finally:
        app.state.engine.dispose()


def test_each_enabled_integration_registers_its_tools_and_nothing_is_contacted(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, repository_source="github", n8n_enabled=True, n8n_webhook_secret="s" * 32, n8n_callback_url="http://127.0.0.1:8000/integrations/n8n/callback")
    wired = integrations_for(settings, None)
    assert wired["repositories"] is not None and wired["n8n_runtime"] is not None and wired["drive"] is None
    app = create_app(settings, llm=FakeLLM(), **wired)
    try:
        names = set(app.state.registry.names)
        assert {"search_repositories", "import_repository", "trigger_workflow", "get_workflow_result"} <= names and "request_drive_authorization" not in names
    finally:
        app.state.engine.dispose()


def test_every_production_entry_point_builds_the_app_with_the_shared_integrations() -> None:
    for path in ENTRY_POINTS:
        calls = re.findall(r"create_app\((.*?)\)\s*(?:,|\n)", path.read_text(encoding="utf-8"), flags=re.DOTALL)
        assert calls, f"{path.name} does not build the app"
        assert all("**integrations_for(" in call for call in calls), f"{path.name} builds the app without integrations_for"
