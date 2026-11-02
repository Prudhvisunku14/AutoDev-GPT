"""The acceptance suite through the REST API: given once with the project, checked, frozen into each run."""

from __future__ import annotations

import hashlib

from fastapi.testclient import TestClient

from app.db.models import Project
from app.demo.fixtures import ACCEPTANCE_SUITE

API = "/api/v1"


def create(client: TestClient, auth: dict[str, str], **body: object) -> object:
    return client.post(f"{API}/projects", json={"name": "calc", "requirement_text": "A calculator.", **body}, headers=auth)


def test_a_project_keeps_its_suite_and_each_run_gets_a_frozen_copy(client: TestClient, auth: dict[str, str]) -> None:
    project = create(client, auth, acceptance=ACCEPTANCE_SUITE.to_dict())
    assert project.status_code == 201 and project.json()["has_acceptance_suite"] is True
    run = client.post(f"{API}/projects/{project.json()['id']}/runs", headers=auth).json()
    with client.app.state.session_factory() as session:
        stored = session.get(Project, project.json()["id"]).acceptance_json
    assert run["acceptance_sha256"] == hashlib.sha256(stored.encode("utf-8")).hexdigest()
    assert client.get(f"{API}/runs/{run['id']}", headers=auth).json()["acceptance_sha256"] == run["acceptance_sha256"]


def test_a_project_without_a_suite_says_so(client: TestClient, auth: dict[str, str]) -> None:
    project = create(client, auth).json()
    assert project["has_acceptance_suite"] is False
    assert client.post(f"{API}/projects/{project['id']}/runs", headers=auth).json()["acceptance_sha256"] is None


def test_a_suite_that_cannot_prove_its_criteria_is_refused_without_echoing_it(client: TestClient, auth: dict[str, str]) -> None:
    suite = ACCEPTANCE_SUITE.to_dict()
    suite["files"] = {"test_acceptance.py": "import unittest\nSECRET_MARKER = 1\n"}
    answer = create(client, auth, acceptance=suite)
    assert answer.status_code == 422 and "no test names AT-001" in answer.text and "SECRET_MARKER" not in answer.text


def test_a_suite_with_unknown_fields_is_refused(client: TestClient, auth: dict[str, str]) -> None:
    assert create(client, auth, acceptance={**ACCEPTANCE_SUITE.to_dict(), "visible_to_provider": True}).status_code == 422


def test_there_is_no_way_to_change_a_suite_after_creation(client: TestClient, auth: dict[str, str]) -> None:
    project = create(client, auth, acceptance=ACCEPTANCE_SUITE.to_dict()).json()
    for method in ("put", "patch"):
        assert getattr(client, method)(f"{API}/projects/{project['id']}", json={"acceptance": None}, headers=auth).status_code == 405


def test_a_criterion_may_be_bound_to_a_component_and_is_frozen_with_it(client: TestClient, auth: dict[str, str]) -> None:
    from tests.unit.test_acceptance import FRONT

    project = create(client, auth, acceptance=FRONT.to_dict())
    assert project.status_code == 201
    with client.app.state.session_factory() as session:
        stored = session.get(Project, project.json()["id"]).acceptance_json
    assert '"component":"frontend"' in stored and stored.count('"component"') == 2


def test_a_component_that_is_not_a_plan_folder_is_refused(client: TestClient, auth: dict[str, str]) -> None:
    from tests.unit.test_acceptance import FRONT

    for name in ("../frontend", "Frontend", "front end", ""):
        suite = FRONT.to_dict()
        suite["criteria"][5]["component"] = name
        assert create(client, auth, acceptance=suite).status_code == 422
