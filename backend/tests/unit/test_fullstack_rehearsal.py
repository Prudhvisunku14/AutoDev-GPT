"""The offline full-stack rehearsal, and the GitHub client wired into the application. Fixtures only: no request leaves
the machine, and React and FastAPI are never run."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.core.egress import EgressPolicy
from app.db.models import Approval, ApprovalStatus
from app.demo import fullstack, weather
from app.orchestrator.fake_llm import FakeLLM, call
from app.repos.source import GitHubSource, build_repository_source
from app.tools.adapters.repositories import RepositoryRuntime, register_repository_tools
from tests.harness import Harness
from tests.helpers import make_settings
from tests.unit.test_coding_tools import pause
from tests.unit.test_github_source import REPO, SHA, Recorder


@pytest.fixture(scope="module")
def rehearsal(tmp_path_factory: pytest.TempPathFactory) -> dict:
    return fullstack.rehearse(tmp_path_factory.mktemp("fullstack"))


def test_every_case_of_the_rehearsal_is_handled(rehearsal: dict) -> None:
    passed = fullstack.checks(rehearsal)
    assert all(passed.values()), [name for name, ok in passed.items() if not ok]


def test_the_final_status_names_each_blocked_component_and_why(rehearsal: dict) -> None:
    detail = rehearsal["run"]["status_detail"]
    assert detail.startswith("verification is blocked: backend (python-fastapi): the fastapi-pytest recipe needs the devpilot/verify-fastapi:1 runtime")
    assert "frontend (react-vite): the react-vitest recipe needs the devpilot/verify-react:1 runtime" in detail


def test_the_repository_client_is_off_unless_configured(tmp_path: Path) -> None:
    egress = EgressPolicy({"github": ["api.github.com"]})
    assert build_repository_source(make_settings(tmp_path), egress) is None
    assert isinstance(build_repository_source(make_settings(tmp_path, repository_source="github"), egress), GitHubSource)
    assert "**integrations_for(settings, sandbox)" in (Path(fullstack.__file__).resolve().parents[1] / "__main__.py").read_text(encoding="utf-8")


def test_the_github_client_works_through_the_tools_and_its_archive_host_stays_refused(tmp_path: Path) -> None:
    """GitHub's answers here are fixtures served in process. The import fails on the egress rule, as it would live."""
    tree = {"tree": [{"path": "requirements.txt", "type": "blob", "size": 9}, {"path": "tests/test_api.py", "type": "blob", "size": 9}], "truncated": False}
    recorder = Recorder({
        "api.github.com/search/repositories": httpx.Response(200, json={"items": [REPO]}), "api.github.com/repos/octo/weather/commits/main": httpx.Response(200, text=SHA),
        f"api.github.com/repos/octo/weather/git/trees/{SHA}": httpx.Response(200, json=tree), "api.github.com/repos/octo/weather/readme": httpx.Response(200, text="# weather fastapi"),
        "api.github.com/repos/octo/weather/license": httpx.Response(200, text="MIT License"), "api.github.com/repos/octo/weather/contents/requirements.txt": httpx.Response(200, text="fastapi\n"),
        f"api.github.com/repos/octo/weather/tarball/{SHA}": httpx.Response(302, headers={"location": f"https://codeload.github.com/octo/weather/legacy.tar.gz/{SHA}"}),
        "api.github.com/repos/octo/weather": httpx.Response(200, json=REPO),
    })  # fmt: skip
    source = GitHubSource(EgressPolicy({"github": ["api.github.com", "github.com"]}), client=httpx.Client(transport=httpx.MockTransport(recorder)))
    steps = [call("submit_analysis", weather.ANALYSIS), call("search_repositories", {"query": "weather fastapi", "must_have": ["fastapi"]}),
             call("import_repository", {"full_name": "octo/weather", "commit": SHA}), pause()]  # fmt: skip
    h = Harness(tmp_path, FakeLLM(*steps))
    register_repository_tools(h.registry, RepositoryRuntime(source))
    run_id = h.new_run(weather.REQUIREMENT, "Weather")
    h.workspace.create_project(h.run(run_id).project_id)
    h.advance(run_id)
    assert recorder.seen == []  # nothing before the person approves
    for _ in range(2):
        with h.sessions() as s:
            approval_id = s.scalars(select(Approval).where(Approval.run_id == run_id, Approval.status == ApprovalStatus.PENDING)).one().id
        h.decide(approval_id, "approve")
        h.advance(run_id)
    outputs = {i.tool: json.loads(i.output_json) for i in h.invocations(run_id) if i.output_json}
    assert outputs["search_repositories"]["data"]["decision"]["full_name"] == "octo/weather" and outputs["search_repositories"]["data"]["source"] == "github"
    assert "codeload.github.com" in outputs["import_repository"]["error"]["summary"] and h.workspace.list_files(h.run(run_id).project_id) == []
    assert {r.url.host for r in recorder.seen} == {"api.github.com"} and all("authorization" not in r.headers for r in recorder.seen)
