"""The evaluation upgrade's draft pull request path: plan, one-use preview, typed confirmation, then one GitHub write.

The package's planning and preview tests are kept as they were. Its publisher tests now run against the existing GitHub
writer (repos/github_write.py) through tests/github_fake.py, an in-process GitHub: no request leaves the process.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.evaluation.github_publish import PreviewStore, PublishRefused, build_plan, publish
from app.main import create_app
from app.orchestrator.fake_llm import FakeLLM
from app.repos.github_write import GitHubWriter
from tests.github_fake import TOKEN, FakeGitHub
from tests.helpers import ALLOWLIST_FILE, API_TOKEN, make_settings

HEADERS = {"Authorization": f"Bearer {API_TOKEN}"}
FILES = {"src/app.py": b"print('hello')\n", "README.md": b"# Demo\n"}


def example_plan(repository: str = "example/demo", branch: str = "devpilot/feature-123") -> Any:
    return build_plan("a" * 32, repository, "main", branch, "Add tested functionality", "changes verified", FILES)


# --- From the package, unchanged in substance -------------------------------------------------------------------


def test_github_plan_rejects_secret_files_and_disallowed_branches() -> None:
    with pytest.raises(PublishRefused):
        build_plan("a" * 32, "example/demo", "main", "main", "Test project", "", {"main.py": b"ok"})
    with pytest.raises(PublishRefused):
        build_plan("a" * 32, "example/demo", "main", "devpilot/new", "Test project", "", {".env": b"OPENAI_API_KEY=unsafe"})
    with pytest.raises(PublishRefused):
        build_plan("a" * 32, "example/demo", "main", "devpilot/new", "Test project", "", {"../../x.py": b"bad"})


def test_preview_is_one_use() -> None:
    store = PreviewStore()
    token = store.save(example_plan())
    assert store.claim(token).repository == "example/demo"
    with pytest.raises(PublishRefused):
        store.claim(token)


# --- Publishing through the existing writer ---------------------------------------------------------------------


def writer(fake: FakeGitHub) -> GitHubWriter:
    from app.core.egress import EgressPolicy

    return GitHubWriter(EgressPolicy.from_file(ALLOWLIST_FILE), TOKEN, client=fake.client())


def test_a_draft_pull_request_on_a_new_branch_keeps_every_other_file() -> None:
    fake = FakeGitHub()
    fake.add_repo("example/demo", {"README.md": "old", "LICENSE": "MIT"})
    before = fake.repos["example/demo"].branches["main"]
    result = publish(example_plan(), writer(fake))
    assert result["draft"] == "true" and result["url"] == "https://github.com/example/demo/pull/1"
    assert fake.repos["example/demo"].pulls[0]["draft"] is True and fake.repos["example/demo"].branches["main"] == before
    assert fake.files_at("example/demo", "devpilot/feature-123") == {"README.md": b"# Demo\n", "LICENSE": b"MIT", "src/app.py": b"print('hello')\n"}
    assert {r.url.host for r in fake.requests} == {"api.github.com"}


def test_an_existing_branch_is_refused_and_no_pull_request_is_opened() -> None:
    fake = FakeGitHub()
    repo = fake.add_repo("example/demo", {"README.md": "old"})
    repo.branches["devpilot/feature-123"] = repo.branches["main"]
    with pytest.raises(PublishRefused, match="never moves an existing branch"):
        publish(example_plan(), writer(fake))
    assert repo.pulls == []


def test_without_push_permission_nothing_is_written() -> None:
    fake = FakeGitHub()
    fake.add_repo("example/demo", {"README.md": "old"}).push = False
    with pytest.raises(PublishRefused, match="cannot push"):
        publish(example_plan(), writer(fake))
    assert [r.method for r in fake.requests] == ["GET"]  # only the repository was read


def test_a_repository_github_does_not_confirm_is_refused() -> None:
    fake = FakeGitHub()
    with pytest.raises(PublishRefused, match="GitHub did not accept"):
        publish(example_plan("example/missing"), writer(fake))


# --- Over HTTP ---------------------------------------------------------------------------------------------------


def evaluation_app(tmp_path: Path, fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch, files: dict[str, bytes] | None = FILES) -> TestClient:
    import app.api.evaluation as route

    from tests.unit.test_github_connection import connection

    app = create_app(make_settings(tmp_path, log_level="ERROR", github_token=None), llm=FakeLLM(), github_connection=connection(tmp_path, fake), github_http=fake.client())
    if files is not None:
        monkeypatch.setattr(route, "verified_snapshot", lambda _request, _session, _run_id: ("independent-acceptance", dict(files)))
    return TestClient(app)


def real_run(client: TestClient) -> str:
    project = client.post("/api/v1/projects", headers=HEADERS, json={"name": "P", "requirement_text": "Create a Python calculator with unit tests"}).json()["id"]
    return client.post(f"/api/v1/projects/{project}/runs", headers=HEADERS).json()["id"]


def preview(client: TestClient, **changes: Any) -> Any:
    changes.setdefault("run_id", real_run(client))
    body = { "repository": "example/demo", "base": "main", "branch": "devpilot/feature-123", "title": "Add tested functionality", "body": "Verified.", **changes}
    return client.post("/api/v1/evaluation/github/preview", headers=HEADERS, json=body)


def test_an_unverified_run_cannot_even_be_previewed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with evaluation_app(tmp_path, FakeGitHub(), monkeypatch, files=None) as client:
        project = client.post("/api/v1/projects", headers=HEADERS, json={"name": "P", "requirement_text": "Create a Python calculator with unit tests"}).json()["id"]
        run = client.post(f"/api/v1/projects/{project}/runs", headers=HEADERS).json()["id"]
        answer = preview(client, run_id=run)
    assert answer.status_code == 409 and "not eligible for a verified delivery" in answer.json()["detail"]


def test_preview_then_typed_confirmation_opens_one_draft_pull_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeGitHub()
    fake.add_repo("example/demo", {"README.md": "old"})
    with evaluation_app(tmp_path, fake, monkeypatch) as client:
        client.app.state.github._store.put("github", {"token": TOKEN, "login": "octo-person", "scopes": ["repo"], "connected_at": 1})  # type: ignore[attr-defined]
        shown = preview(client).json()
        assert shown["confirmation_required"] == "publish example/demo:devpilot/feature-123" and shown["github_account"] == "octo-person"
        assert [f["path"] for f in shown["preview"]["files"]] == ["README.md", "src/app.py"] and fake.requests == []  # nothing written by a preview
        wrong = client.post("/api/v1/evaluation/github/publish", headers=HEADERS, json={"preview_id": shown["preview_id"], "confirmation": "yes"})
        assert wrong.status_code == 409 and "exact repository" in wrong.json()["detail"] and fake.requests == []
        shown = preview(client).json()  # a refused confirmation used the preview up
        done = client.post("/api/v1/evaluation/github/publish", headers=HEADERS, json={"preview_id": shown["preview_id"], "confirmation": shown["confirmation_required"]})
        assert done.status_code == 200, done.text
        again = client.post("/api/v1/evaluation/github/publish", headers=HEADERS, json={"preview_id": shown["preview_id"], "confirmation": shown["confirmation_required"]})
    assert done.json()["url"] == "https://github.com/example/demo/pull/1" and len(fake.repos["example/demo"].pulls) == 1
    assert again.status_code == 409 and "already been used" in again.json()["detail"]


def test_files_changed_after_the_preview_are_not_published(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.api.evaluation as route

    fake = FakeGitHub()
    fake.add_repo("example/demo", {"README.md": "old"})
    with evaluation_app(tmp_path, fake, monkeypatch) as client:
        client.app.state.github._store.put("github", {"token": TOKEN, "login": "octo-person", "scopes": ["repo"], "connected_at": 1})  # type: ignore[attr-defined]
        shown = preview(client).json()
        monkeypatch.setattr(route, "verified_snapshot", lambda *_a: ("independent-acceptance", {**FILES, "src/app.py": b"swapped\n"}))
        answer = client.post("/api/v1/evaluation/github/publish", headers=HEADERS, json={"preview_id": shown["preview_id"], "confirmation": shown["confirmation_required"]})
    assert answer.status_code == 409 and "changed after the preview" in answer.json()["detail"] and fake.requests == []


def test_without_a_github_connection_publishing_is_refused_before_any_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeGitHub()
    with evaluation_app(tmp_path, fake, monkeypatch) as client:
        shown = preview(client).json()
        answer = client.post("/api/v1/evaluation/github/publish", headers=HEADERS, json={"preview_id": shown["preview_id"], "confirmation": shown["confirmation_required"]})
    assert answer.status_code == 409 and "GitHub is not connected" in answer.json()["detail"] and fake.requests == []


def test_the_routes_need_authentication(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with evaluation_app(tmp_path, FakeGitHub(), monkeypatch) as client:
        assert client.get("/api/v1/evaluation/tools").status_code == 401
        assert client.post("/api/v1/evaluation/github/preview", json={}).status_code == 401
        listing = client.get("/api/v1/evaluation/tools", headers=HEADERS).json()
        assert "compare_tools" in [t["name"] for t in listing["tools"]] and "not proof" in listing["warning"]
        diff = client.post("/api/v1/evaluation/document-diff", headers=HEADERS, json={"previous_text": "Deadline 7 days", "current_text": "Deadline 10 days", "previous_label": "v1", "current_label": "v2"}).json()
        assert diff["changed"] and diff["interpretation"] == "not performed"
