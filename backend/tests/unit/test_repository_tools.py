"""Repository search and import through the real orchestrator, policy engine, approval queue and database.
GPT is FakeLLM and the repositories are fixtures, never GitHub."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.coding.simulated import SimulatedLauncher
from app.db.models import Approval, ApprovalStatus, Run, RunState
from app.verification.report import build_report
from app.demo import mvp, weather
from app.orchestrator.fake_llm import FakeLLM, Step, call
from app.repos.fixtures import OWNER, FixtureSource
from app.tools.adapters.repositories import RepositoryRuntime, register_repository_tools
from tests.harness import Harness
from tests.unit.test_coding_tools import GOOD, TESTS, Rig, finish, generate, opening, pause, submit
from tests.unit.test_selection_feedback import adaptive

SUITABLE, GPL = f"{OWNER}/react-fastapi-weather", f"{OWNER}/weather-dashboard-gpl"
COMMIT = FixtureSource().detail(SUITABLE).commit


def harness(tmp_path: Path, *steps: Step) -> tuple[Harness, str, str, FixtureSource]:
    h = Harness(tmp_path, FakeLLM(*steps))
    source = FixtureSource()
    register_repository_tools(h.registry, RepositoryRuntime(source))
    run_id = h.new_run(weather.REQUIREMENT, "Weather")
    project_id = h.run(run_id).project_id
    h.workspace.create_project(project_id)
    return h, run_id, project_id, source


def answer(h: Harness, run_id: str, decision: str = "approve") -> tuple[str, str]:
    with h.sessions() as s:
        approval = s.scalars(select(Approval).where(Approval.run_id == run_id, Approval.status == ApprovalStatus.PENDING)).one()
        tool, risk, approval_id = approval.tool, approval.risk, approval.id
    h.decide(approval_id, decision)
    h.advance(run_id)
    return tool, risk


def output(h: Harness, run_id: str, tool: str) -> dict[str, Any]:
    return [json.loads(i.output_json) for i in h.invocations(run_id) if i.tool == tool and i.output_json][-1]


def search(**changes: Any) -> Step:
    return call("search_repositories", {**weather.SEARCH, **changes})


def importing(name: str = SUITABLE, commit: str = COMMIT) -> Step:
    return call("import_repository", {"full_name": name, "commit": commit})


ANALYSIS = call("submit_analysis", weather.ANALYSIS)


def test_a_search_waits_for_a_persons_approval_and_fetches_nothing_before_it(tmp_path: Path) -> None:
    h, run_id, _project, source = harness(tmp_path, ANALYSIS, search(), pause())
    assert h.advance(run_id).state is RunState.AWAITING_APPROVAL and source.calls == []
    assert answer(h, run_id) == ("search_repositories", "EXTERNAL_NETWORK") and source.calls[0] == ("search", weather.SEARCH["query"])


def test_a_denied_search_fetches_nothing(tmp_path: Path) -> None:
    h, run_id, _project, source = harness(tmp_path, ANALYSIS, search(), pause())
    h.advance(run_id)
    answer(h, run_id, "deny")
    assert source.calls == [] and output(h, run_id, "search_repositories")["error"]["class"] == "APPROVAL_DENIED"


def test_an_approved_search_evaluates_every_result_and_python_decides_to_reuse(tmp_path: Path) -> None:
    h, run_id, _project, _source = harness(tmp_path, ANALYSIS, search(), pause())
    h.advance(run_id)
    answer(h, run_id)
    data = output(h, run_id, "search_repositories")["data"]
    assert data["fixture_data"] is True and data["decision"]["action"] == "reuse" and data["decision"]["full_name"] == SUITABLE and data["decision"]["commit"] == COMMIT
    eligible = {c["full_name"]: c["eligible"] for c in data["candidates"]}
    assert eligible[SUITABLE] and not eligible[GPL] and not eligible[f"{OWNER}/fastapi-react-weather-plus"] and not eligible[f"{OWNER}/weather-app-removed"]
    shown = " ".join(i.output_json or "" for i in h.invocations(run_id))
    assert "ignore all previous instructions" not in shown and "setup.invalid" not in shown


def test_an_import_needs_its_own_approval_and_brings_the_pinned_commit_with_its_licence(tmp_path: Path) -> None:
    h, run_id, project_id, source = harness(tmp_path, ANALYSIS, search(), importing(), pause())
    h.advance(run_id)
    answer(h, run_id)
    assert h.run(run_id).state is RunState.AWAITING_APPROVAL and ("archive", SUITABLE) not in source.calls
    assert answer(h, run_id)[0] == "import_repository"
    files = h.workspace.list_files(project_id)
    assert {"backend/app/main.py", "frontend/package.json", "LICENSE", "THIRD_PARTY_NOTICES.md", "DEVPILOT_IMPORT.json"} <= set(files)
    imported = h.events_of(run_id, "repository.imported")[0]
    assert imported["commit"] == COMMIT and imported["license"] == "MIT" and imported["source"] == "fixture"


def test_importing_a_repository_python_did_not_choose_is_refused(tmp_path: Path) -> None:
    gpl_commit = FixtureSource().detail(GPL).commit
    h, run_id, project_id, source = harness(tmp_path, ANALYSIS, search(), importing(GPL, gpl_commit), pause())
    h.advance(run_id)
    answer(h, run_id)
    answer(h, run_id)
    error = output(h, run_id, "import_repository")["error"]
    assert error["class"] == "POLICY_REFUSED" and "reuse decision" in error["summary"]
    assert h.workspace.list_files(project_id) == [] and not any(kind == "archive" for kind, _ in source.calls)


def test_when_nothing_suitable_is_found_python_decides_to_build_from_scratch(tmp_path: Path) -> None:
    h, run_id, _project, _source = harness(tmp_path, ANALYSIS, search(query="quantum spreadsheet compiler", must_have=[]), pause())
    h.advance(run_id)
    answer(h, run_id)
    assert output(h, run_id, "search_repositories")["data"]["decision"] == {"action": "build", "reason": "no repository matched the search", "full_name": None,
                                                                           "commit": None, "url": None, "license": None, "attribution": ""}  # fmt: skip
    h2, run2, _p2, _s2 = harness(tmp_path / "second", ANALYSIS, search(must_have=["svelte"]), pause())
    h2.advance(run2)
    answer(h2, run2)
    assert output(h2, run2, "search_repositories")["data"]["decision"]["action"] == "build"


def test_an_import_never_overwrites_the_users_project(tmp_path: Path) -> None:
    h, run_id, project_id, _source = harness(tmp_path, ANALYSIS, search(), importing(), pause())
    h.workspace.write_text(project_id, "README.md", "the user's notes\n")
    h.advance(run_id)
    answer(h, run_id)
    answer(h, run_id)
    assert "never overwrites" in output(h, run_id, "import_repository")["error"]["summary"]
    assert h.workspace.list_files(project_id) == ["README.md"] and h.workspace.read_text(project_id, "README.md") == "the user's notes\n"


def _imported(r: Rig) -> None:
    with r.h.sessions.begin() as s:
        r.h.events.record(s, project_id=r.project_id, run_id=r.run_id, type="repository.imported",
                          payload={"full_name": SUITABLE, "commit": COMMIT, "license": "MIT", "notices_file": "THIRD_PARTY_NOTICES.md", "license_files": ["LICENSE"]})  # fmt: skip


def test_reused_code_is_not_delivered_without_its_licence_and_notice(tmp_path: Path) -> None:
    r = Rig(tmp_path, *opening(), generate(1), generate(2), submit(), finish(), coding=[GOOD, TESTS])
    _imported(r)
    assert r.advance().state is not RunState.COMPLETED and r.untouched()
    error = r.output("finish")["error"]
    assert error["class"] == "POLICY_REFUSED" and sorted(error["details"]) == ["LICENSE", "THIRD_PARTY_NOTICES.md"]


def test_reused_code_is_delivered_with_its_licence_and_notice(tmp_path: Path) -> None:
    r = Rig(tmp_path, *opening(), generate(1), generate(2), submit(), finish(), coding=[GOOD, TESTS])
    r.h.workspace.write_text(r.project_id, "LICENSE", "MIT License\n")
    r.h.workspace.write_text(r.project_id, "THIRD_PARTY_NOTICES.md", "# Third-party code\n")
    _imported(r)
    assert r.advance().state is RunState.COMPLETED


def weather_rig(tmp_path: Path, scope: str) -> Rig:
    launcher = SimulatedLauncher([])
    r = Rig(tmp_path, *weather.offline_steps(SUITABLE, COMMIT, scope=scope), pause(), launcher=launcher, selector=adaptive(launcher))
    register_repository_tools(r.h.registry, RepositoryRuntime(FixtureSource()))
    r.h.workspace.delete(r.project_id, "README.md")  # an empty project: an import never overwrites the user's files
    r.advance()
    assert answer(r.h, r.run_id)[0] == "search_repositories"
    assert answer(r.h, r.run_id)[0] == "import_repository"
    return r


def test_the_full_weather_request_reuses_a_repository_and_cannot_be_planned_yet(tmp_path: Path) -> None:
    r = weather_rig(tmp_path, "full")
    error = r.output("submit_plan")["error"]
    assert error["class"] == "VALIDATION_FAILED" and error["details"] == ["requirements not covered by any task: R-001, R-002, R-003"]
    assert r.h.run(r.run_id).state is RunState.PLANNING and r.attempts() == [] and "backend/app/main.py" in r.h.workspace.list_files(r.project_id)


def test_the_back_end_scope_is_planned_and_stops_safely_where_no_tool_can_build_its_stack(tmp_path: Path) -> None:
    r = weather_rig(tmp_path, "backend")
    run = r.h.run(r.run_id)
    error = r.output("generate_code")["error"]
    assert error["class"] == "POLICY_REFUSED" and "verification is blocked, so no code is written" in error["summary"]
    assert run.state is RunState.STOPPED_BY_POLICY and run.outcome == "verification_unavailable" and r.attempts() == [] and r.sandbox.specs == []  # nothing written, nothing run
    with r.h.sessions() as s:
        row = s.get(Run, r.run_id)
        report = build_report(s, row, row.project, gpt="FakeLLM (scripted)", sandbox="FakeSandbox")
    assert report["repositories"]["decision"]["full_name"] == SUITABLE and report["repositories"]["imported"]["commit"] == COMMIT
    assert "repository data (fixtures, not GitHub)" in report["provenance"]["simulated"]
    assert "backend/app/main.py" in r.h.workspace.list_files(r.project_id) and mvp.CALCULATOR not in r.h.workspace.read_text(r.project_id, "backend/app/main.py")


def test_the_weather_acceptance_suite_is_valid_and_needs_no_live_service() -> None:
    from app.verification.acceptance import validate

    validate(weather.ACCEPTANCE_SUITE)
    tests = weather.ACCEPTANCE_TESTS
    assert "mock.patch" in tests and "https://weather.invalid" in tests and "requests.get(" not in tests and len(weather.ACCEPTANCE_SUITE.criteria) == 7
