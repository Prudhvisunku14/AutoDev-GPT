from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

API = "/api/v1"


def _create_project(client: TestClient, auth: dict[str, str], **body: Any) -> dict[str, Any]:
    payload = {"name": "Todo app", "requirement_text": "A FastAPI todo list with CRUD."} | body
    response = client.post(f"{API}/projects", json=payload, headers=auth)
    assert response.status_code == 201, response.text
    return response.json()


def test_create_project_persists_and_creates_workspace(
    client: TestClient, app: FastAPI, auth: dict[str, str]
) -> None:
    project = _create_project(client, auth, name="  Todo app  ")
    assert project["name"] == "Todo app"  # trimmed
    assert project["status"] == "ACTIVE"
    assert project["stack"] is None
    assert "workspace_path" not in project  # host paths are not exposed
    assert (app.state.workspace_fs.root / project["id"]).is_dir()

    fetched = client.get(f"{API}/projects/{project['id']}", headers=auth).json()
    assert fetched == project


def test_create_project_rejects_a_known_but_unverifiable_stack(client: TestClient, auth: dict[str, str]) -> None:
    response = client.post(
        f"{API}/projects",
        json={"name": "UI", "requirement_text": "Build a front end", "stack": "react-vite"},
        headers=auth,
    )
    assert response.status_code == 422
    assert "cannot be built and verified" in response.json()["detail"]


@pytest.mark.parametrize(
    "body",
    [
        {"name": "", "requirement_text": "r"},
        {"name": "   ", "requirement_text": "r"},
        {"name": "n", "requirement_text": ""},
        {"name": "n" * 201, "requirement_text": "r"},
        {"name": "n", "requirement_text": "r" * 20_001},
        {"name": "n"},
        {"name": "n", "requirement_text": "r", "stack": "not-a-stack"},
        {"name": 5, "requirement_text": "r"},
    ],
)
def test_invalid_project_bodies_are_rejected(client: TestClient, auth: dict[str, str], body: dict[str, Any]) -> None:
    assert client.post(f"{API}/projects", json=body, headers=auth).status_code == 422


def test_list_projects_with_pagination(client: TestClient, auth: dict[str, str]) -> None:
    ids = {_create_project(client, auth, name=f"p{i}")["id"] for i in range(3)}
    page = client.get(f"{API}/projects?limit=2", headers=auth).json()
    rest = client.get(f"{API}/projects?limit=2&offset=2", headers=auth).json()
    assert len(page) == 2 and len(rest) == 1
    assert {p["id"] for p in page + rest} == ids
    assert client.get(f"{API}/projects?limit=0", headers=auth).status_code == 422
    assert client.get(f"{API}/projects?limit=201", headers=auth).status_code == 422


def test_backend_wide_run_list_includes_project_names(client: TestClient, auth: dict[str, str]) -> None:
    projects = [_create_project(client, auth, name=f"Project {i}") for i in range(2)]
    runs = [client.post(f"{API}/projects/{project['id']}/runs", headers=auth).json() for project in projects]
    response = client.get(f"{API}/runs?limit=1", headers=auth)
    assert response.status_code == 200
    assert len(response.json()) == 1
    all_runs = client.get(f"{API}/runs", headers=auth).json()
    assert {row["project_name"] for row in all_runs} == {"Project 0", "Project 1"}
    assert {row["id"] for row in all_runs} == {run["id"] for run in runs}


def test_budget_and_capabilities_report_enforced_backend_state(client: TestClient, auth: dict[str, str]) -> None:
    budget = client.get(f"{API}/budget", headers=auth)
    capabilities = client.get(f"{API}/capabilities", headers=auth)
    assert budget.status_code == capabilities.status_code == 200
    assert budget.json()["run_limit_usd"] > 0
    assert budget.json()["cost_known"] is False
    assert "neither calculated nor limited" in budget.json()["estimate"]
    stacks = {stack["id"]: stack for stack in capabilities.json()["stacks"]}
    assert "python-cli" in stacks
    assert stacks["react-vite"]["verifiable"] is False
    assert "OPENCODE_ENABLED is false" in capabilities.json()["coding"]["detail"]
    assert capabilities.json()["can_build"] is False


def test_unknown_prices_block_a_start_even_when_the_legacy_approval_setting_is_off(
    client: TestClient, auth: dict[str, str]
) -> None:
    project = _create_project(client, auth)
    run = client.post(f"{API}/projects/{project['id']}/runs", headers=auth).json()

    response = client.post(f"{API}/runs/{run['id']}/start", headers=auth)

    assert response.status_code == 409
    assert "No spending limit can be enforced" in response.json()["detail"]
    assert client.get(f"{API}/runs/{run['id']}", headers=auth).json()["state"] == "CREATED"


def test_dashboard_selected_stack_requires_explicit_spend_approval(client: TestClient, auth: dict[str, str]) -> None:
    project = _create_project(client, auth, stack="python-cli")
    run = client.post(f"{API}/projects/{project['id']}/runs", headers=auth).json()
    response = client.post(f"{API}/runs/{run['id']}/start", headers=auth)
    assert response.status_code == 409
    assert "spending approval" in response.json()["detail"]
    assert client.get(f"{API}/runs/{run['id']}", headers=auth).json()["state"] == "CREATED"


def test_backend_rejects_an_approval_above_its_enforced_limit(client: TestClient, app: FastAPI, auth: dict[str, str]) -> None:
    app.state.settings.openai_price_input_per_mtok = 1
    app.state.settings.openai_price_output_per_mtok = 1
    project = _create_project(client, auth, stack="python-cli")
    run = client.post(f"{API}/projects/{project['id']}/runs", headers=auth).json()
    limit = app.state.settings.run_budget_usd

    response = client.post(f"{API}/runs/{run['id']}/start", json={"approved_max_usd": limit + 0.01}, headers=auth)

    assert response.status_code == 409
    assert "but this run's enforced limit is" in response.json()["detail"]
    assert client.get(f"{API}/runs/{run['id']}", headers=auth).json()["state"] == "CREATED"


def test_backend_refuses_approval_when_prices_cannot_enforce_a_spending_cap(client: TestClient, auth: dict[str, str]) -> None:
    project = _create_project(client, auth, stack="python-cli")
    run = client.post(f"{API}/projects/{project['id']}/runs", headers=auth).json()

    response = client.post(f"{API}/runs/{run['id']}/start", json={"approved_max_usd": 2.0}, headers=auth)

    assert response.status_code == 409
    assert "No spending limit can be enforced" in response.json()["detail"]
    assert client.get(f"{API}/runs/{run['id']}", headers=auth).json()["state"] == "CREATED"


@pytest.mark.parametrize("project_id", ["f" * 32, "not-an-id", "..%2F..%2Fetc"])
def test_unknown_or_malformed_project_is_404(client: TestClient, auth: dict[str, str], project_id: str) -> None:
    assert client.get(f"{API}/projects/{project_id}", headers=auth).status_code == 404
    assert client.post(f"{API}/projects/{project_id}/runs", headers=auth).status_code == 404


def test_run_lifecycle_and_events(client: TestClient, auth: dict[str, str]) -> None:
    project = _create_project(client, auth)
    pid = project["id"]

    created = client.post(f"{API}/projects/{pid}/runs", headers=auth)
    assert created.status_code == 201
    run = created.json()
    assert run["state"] == "CREATED"
    assert run["project_id"] == pid
    assert run["ended_at"] is None

    # One active run per project.
    conflict = client.post(f"{API}/projects/{pid}/runs", headers=auth)
    assert conflict.status_code == 409

    assert client.get(f"{API}/runs/{run['id']}", headers=auth).json() == run
    assert [r["id"] for r in client.get(f"{API}/projects/{pid}/runs", headers=auth).json()] == [run["id"]]

    cancelled = client.post(f"{API}/runs/{run['id']}/cancel", headers=auth)
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "CANCELLED"
    assert cancelled.json()["ended_at"] is not None
    assert client.post(f"{API}/runs/{run['id']}/cancel", headers=auth).status_code == 409

    # After cancellation a new run may start.
    second = client.post(f"{API}/projects/{pid}/runs", headers=auth)
    assert second.status_code == 201

    events = client.get(f"{API}/runs/{run['id']}/events", headers=auth).json()
    assert [e["type"] for e in events] == ["run.created", "run.cancelled"]
    assert [e["seq"] for e in events] == [2, 3]  # seq 1 is project.created
    assert events[1]["payload"] == {"previous_state": "CREATED"}
    later = client.get(f"{API}/runs/{run['id']}/events?after_seq=2", headers=auth).json()
    assert [e["type"] for e in later] == ["run.cancelled"]


@pytest.mark.parametrize("run_id", ["f" * 32, "bad"])
def test_unknown_run_is_404(client: TestClient, auth: dict[str, str], run_id: str) -> None:
    for method, path in (
        ("GET", f"{API}/runs/{run_id}"),
        ("POST", f"{API}/runs/{run_id}/cancel"),
        ("GET", f"{API}/runs/{run_id}/events"),
    ):
        assert client.request(method, path, headers=auth).status_code == 404


def test_workspace_files_are_listed_and_read_through_the_jail(
    client: TestClient, app: FastAPI, auth: dict[str, str]
) -> None:
    pid = _create_project(client, auth)["id"]
    app.state.workspace_fs.write_text(pid, "app/main.py", "print('hello')\n")

    files = client.get(f"{API}/projects/{pid}/files", headers=auth).json()
    assert files == {"files": ["app/main.py"]}

    content = client.get(f"{API}/projects/{pid}/files/content", params={"path": "app/main.py"}, headers=auth)
    assert content.json() == {"path": "app/main.py", "content": "print('hello')\n"}


@pytest.mark.parametrize(
    ("path", "status_code"),
    [
        ("../../../Windows/win.ini", 400),
        ("..\\..\\secret.txt", 400),
        ("C:\\Windows\\win.ini", 400),
        ("/etc/passwd", 400),
        ("\\\\server\\share\\x", 400),
        ("file.txt:stream", 400),
        ("missing.txt", 404),
    ],
)
def test_file_reads_outside_the_jail_are_refused(
    client: TestClient, auth: dict[str, str], path: str, status_code: int
) -> None:
    pid = _create_project(client, auth)["id"]
    response = client.get(f"{API}/projects/{pid}/files/content", params={"path": path}, headers=auth)
    assert response.status_code == status_code


def test_oversize_file_read_is_413(client: TestClient, app: FastAPI, auth: dict[str, str]) -> None:
    pid = _create_project(client, auth)["id"]
    limit = app.state.settings.workspace_max_file_bytes
    (app.state.workspace_fs.root / pid / "big.txt").write_bytes(b"x" * (limit + 1))
    response = client.get(f"{API}/projects/{pid}/files/content", params={"path": "big.txt"}, headers=auth)
    assert response.status_code == 413


def test_files_of_unknown_project_are_404(client: TestClient, auth: dict[str, str]) -> None:
    assert client.get(f"{API}/projects/{'f' * 32}/files", headers=auth).status_code == 404


def test_missing_workspace_directory_is_reported_as_404(
    client: TestClient, app: FastAPI, auth: dict[str, str]
) -> None:
    pid = _create_project(client, auth)["id"]
    (app.state.workspace_fs.root / pid).rmdir()
    assert client.get(f"{API}/projects/{pid}/files", headers=auth).status_code == 404


def test_status_reports_components(client: TestClient, auth: dict[str, str]) -> None:
    _create_project(client, auth)
    body = client.get(f"{API}/status", headers=auth).json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["workspaces_writable"] is True
    assert body["projects"] == 1 and body["runs"] == 0
    assert "backend" in body["egress_components"]
    assert body["phase"].startswith("2")


def test_responses_carry_a_request_id(client: TestClient) -> None:
    assert len(client.get(f"{API}/health").headers["X-Request-ID"]) == 12
