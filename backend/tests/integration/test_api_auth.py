"""Every route except /health requires the bearer token (SECURITY T7)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.helpers import API_TOKEN

PID = "0" * 32

PROTECTED = [
    ("GET", "/api/v1/status"),
    ("POST", "/api/v1/projects"),
    ("GET", "/api/v1/projects"),
    ("GET", f"/api/v1/projects/{PID}"),
    ("GET", f"/api/v1/projects/{PID}/files"),
    ("GET", f"/api/v1/projects/{PID}/files/content?path=a.txt"),
    ("POST", f"/api/v1/projects/{PID}/runs"),
    ("GET", f"/api/v1/projects/{PID}/runs"),
    ("GET", f"/api/v1/runs/{PID}"),
    ("POST", f"/api/v1/runs/{PID}/cancel"),
    ("GET", f"/api/v1/runs/{PID}/events"),
]

BAD_HEADERS = [
    {},
    {"Authorization": ""},
    {"Authorization": "Bearer"},
    {"Authorization": "Bearer wrong-token"},
    {"Authorization": f"Basic {API_TOKEN}"},
    {"Authorization": f"Bearer {API_TOKEN}x"},
    {"Authorization": f"Bearer {API_TOKEN[:-1]}"},
]


@pytest.mark.parametrize(("method", "path"), PROTECTED)
@pytest.mark.parametrize("headers", BAD_HEADERS)
def test_protected_routes_answer_401(
    client: TestClient, method: str, path: str, headers: dict[str, str]
) -> None:
    response = client.request(method, path, headers=headers, json={"name": "n", "requirement_text": "r"})
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_health_needs_no_token(client: TestClient) -> None:
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_scheme_is_case_insensitive(client: TestClient) -> None:
    response = client.get("/api/v1/status", headers={"Authorization": f"bearer {API_TOKEN}"})
    assert response.status_code == 200


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_interactive_docs_are_disabled(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 404


def test_cors_allows_only_the_dashboard_origin(client: TestClient) -> None:
    allowed = client.options(
        "/api/v1/projects",
        headers={"Origin": "http://127.0.0.1:5173", "Access-Control-Request-Method": "GET"},
    )
    assert allowed.headers.get("access-control-allow-origin") == "http://127.0.0.1:5173"
    denied = client.options(
        "/api/v1/projects",
        headers={"Origin": "http://evil.example.com", "Access-Control-Request-Method": "GET"},
    )
    assert "access-control-allow-origin" not in denied.headers
