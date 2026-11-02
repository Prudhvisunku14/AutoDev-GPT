"""The dashboard: static files, served without a token, that read the API with one. Nothing external, no HTML built
from data, and no control that only looks functional."""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from app.main import UI_DIR


def test_the_dashboard_is_served_and_the_api_still_needs_the_token(client: TestClient) -> None:
    page = client.get("/ui/")
    assert page.status_code == 200 and "DevPilot AI" in page.text
    assert client.get("/ui/app.js").status_code == 200 and client.get("/ui/app.css").status_code == 200
    assert client.get("/api/v1/projects").status_code == 401


def test_it_loads_nothing_from_elsewhere() -> None:
    for path in UI_DIR.iterdir():
        assert not re.search(r"https?://", path.read_text(encoding="utf-8")), path.name
    assert "default-src 'self'" in (UI_DIR / "index.html").read_text(encoding="utf-8")


def test_data_never_becomes_html() -> None:
    script = (UI_DIR / "app.js").read_text(encoding="utf-8")
    for unsafe in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert unsafe not in script


def test_every_control_does_something_real() -> None:
    page, script = (UI_DIR / "index.html").read_text(encoding="utf-8"), (UI_DIR / "app.js").read_text(encoding="utf-8")
    assert re.findall(r'<button id="(\w+)"', page) == ["refresh"] and 'getElementById("refresh").addEventListener' in script
    assert '<form id="login-form">' in page and 'getElementById("login-form").addEventListener("submit"' in script
    assert not re.search(r"method:\s*\"(POST|PUT|PATCH|DELETE)\"", script)  # it only reads


def test_it_says_when_a_run_was_simulated() -> None:
    script = (UI_DIR / "app.js").read_text(encoding="utf-8")
    assert "Simulated in this run" in script and "FakeLLM" in script and "never shown to the coding provider" in script
