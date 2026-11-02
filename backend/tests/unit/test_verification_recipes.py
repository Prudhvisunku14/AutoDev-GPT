"""Verification recipes: fixed commands, checked folders, declared runtimes and parsers. Nothing here runs a framework:
the FastAPI and React outputs below are fixture text, and their runtimes have never been built."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.db.models import RunState
from app.demo import weather
from app.repos.fixtures import FixtureSource
from app.sandbox import policy
from app.sandbox.policy import APPROVED_COMMANDS
from app.verification import recipes
from tests.helpers import make_settings
from app.verification.results import Criterion, CriterionStatus, judge_criteria
from tests.unit.test_coding_tools import Rig, generate, pause
from app.orchestrator.fake_llm import call

PYTEST_OUTPUT = """============================= test session starts ==============================
collected 3 items

tests/test_api.py::test_at_001_returns_the_weather PASSED                [ 33%]
tests/test_api.py::WeatherTests::test_at_004_unknown_city_is_404 FAILED  [ 66%]
tests/test_api.py::test_ac_002_invalid_city[empty] PASSED                [100%]

=========================== short test summary info ============================
FAILED tests/test_api.py::WeatherTests::test_at_004_unknown_city_is_404 - AssertionError
========================= 1 failed, 2 passed in 0.42s ==========================
"""
VITEST_OUTPUT = json.dumps({"numTotalTests": 2, "numPassedTests": 1, "numFailedTests": 1, "numPendingTests": 0, "testResults": [{"assertionResults": [
    {"title": "AT-007 calls the API through VITE_API_BASE_URL", "fullName": "AT-007 calls the API through VITE_API_BASE_URL", "status": "passed"},
    {"title": "shows an error state", "fullName": "Dashboard shows an error state", "status": "failed"}]}]})  # fmt: skip


def test_each_stack_has_its_recipe_and_only_the_built_runtime_is_approved() -> None:
    assert {s: recipes.readiness(s)[0] for s in ("python-cli", "python-fastapi", "react-vite", "static-html")} == {"python-cli": True, "python-fastapi": False, "react-vite": False, "static-html": False}
    assert "devpilot/verify-fastapi:1" in recipes.readiness("python-fastapi")[1] and "defined but not built or approved" in recipes.readiness("react-vite")[1]
    assert recipes.readiness("static-html")[1] == "no verification recipe exists for stack static-html"


def test_every_recipe_declares_its_whole_contract() -> None:
    for recipe in recipes.RECIPES.values():
        r = recipe.runtime
        assert recipe.argv and recipe.result_format and recipe.failure_conditions and recipe.acceptance and r.image and r.network == "none" and r.memory and r.cpus and r.pids and r.timeout_seconds


def test_commands_are_fixed_lists_with_a_checked_folder_and_no_shell() -> None:
    assert recipes.command_for(recipes.RECIPES["python-cli"], None) == APPROVED_COMMANDS["python-unittest"].argv
    assert recipes.command_for(recipes.RECIPES["python-fastapi"], "backend")[-1] == "/workspace/backend"
    for recipe in recipes.RECIPES.values():
        assert isinstance(recipe.argv, tuple) and not {"sh", "bash", "-c", "cmd", "powershell"} & set(recipe.argv)


@pytest.mark.parametrize("folder", ["../etc", "backend/app", "", None, "Backend", "a b", "/abs"])
def test_a_folder_that_is_not_one_plain_level_is_refused(folder: str | None) -> None:
    with pytest.raises(recipes.RecipeError):
        recipes.command_for(recipes.RECIPES["react-vite"], folder)


def test_gpt_cannot_reach_a_recipe_command_through_the_sandbox_tool() -> None:
    names = {r.name for r in recipes.RECIPES.values()}
    assert not (names - {"python-unittest"}) & set(APPROVED_COMMANDS)


def test_a_project_needing_a_package_the_runtime_lacks_is_refused_before_running() -> None:
    fastapi, react = recipes.RECIPES["python-fastapi"], recipes.RECIPES["react-vite"]
    ok = {"backend/requirements.txt": b"fastapi==0.115.0\nhttpx>=0.27\n# a comment\n"}
    bad = {"backend/requirements.txt": b"fastapi\nrequests==2.32\n"}
    assert recipes.dependency_problems(fastapi, ok, "backend") == []
    assert recipes.dependency_problems(fastapi, bad, "backend") == ["backend/requirements.txt needs requests, which devpilot/verify-fastapi:1 does not provide"]
    assert recipes.dependency_problems(fastapi, {}, "backend") == ["backend/requirements.txt is missing"]
    widget = {"frontend/package.json": json.dumps({"dependencies": {"react": "1", "left-pad": "1"}}).encode()}
    assert recipes.dependency_problems(react, widget, "frontend") == ["frontend/package.json needs left-pad, which devpilot/verify-react:1 does not provide"]


def test_a_reused_repository_is_checked_against_its_runtime() -> None:
    detail = FixtureSource().detail("fixture-labs/fastapi-weather-api")
    files = {f"backend/{path}": text.encode() for path, text in detail.manifests.items()}
    assert recipes.dependency_problems(recipes.RECIPES["python-fastapi"], files, "backend") == []


def test_pytest_output_is_parsed_and_mapped_to_criteria() -> None:
    counts = recipes.parse_pytest(PYTEST_OUTPUT)
    assert counts.readable and (counts.ran, counts.failures, counts.passed) == (3, 1, 2) and counts.failed_names == ("test_at_004_unknown_city_is_404",)
    judged = {c.code: c.status for c in judge_criteria([Criterion("AT-001", "MUST"), Criterion("AT-004", "MUST"), Criterion("AC-002", "MUST")], counts)}
    assert judged == {"AT-001": CriterionStatus.PASSED, "AT-004": CriterionStatus.FAILED, "AC-002": CriterionStatus.PASSED}


def test_vitest_json_is_parsed_and_its_names_map_to_criteria() -> None:
    counts = recipes.parse_vitest_json("some log line\n" + VITEST_OUTPUT)
    assert counts.readable and (counts.ran, counts.failures) == (2, 1) and counts.passed_names == ("at_007_calls_the_api_through_vite_api_base_url",)
    assert judge_criteria([Criterion("AT-007", "SHOULD")], counts)[0].status is CriterionStatus.PASSED


@pytest.mark.parametrize("parser", [recipes.parse_pytest, recipes.parse_vitest_json])
def test_output_that_cannot_be_read_is_never_a_pass(parser: object) -> None:
    assert parser("Traceback (most recent call last):\nKilled\n").readable is False  # type: ignore[operator]


def test_a_stack_without_an_approved_runtime_stops_the_run_with_a_precise_status(tmp_path: Path) -> None:
    opening = [call("submit_analysis", weather.BACKEND_ANALYSIS), call("submit_plan", weather.BACKEND_PLAN), call("submit_criteria", weather.CRITERIA)]
    r = Rig(tmp_path, *opening, generate(1), pause())
    run = r.advance()
    assert run.state is RunState.STOPPED_BY_POLICY and run.outcome == "verification_unavailable" and r.attempts() == [] and r.sandbox.specs == []
    blocked = r.report()["verification_blocked"]["components"]
    assert blocked == [{"component": "the project", "stack": "python-fastapi", "reason": recipes.readiness("python-fastapi")[1]}]


# --- Fixed commands for the framework runtimes (built offline; never run on Docker) ---------------------------------


def test_fastapi_runs_with_devpilots_own_config_inside_the_checked_folder_only() -> None:
    argv = recipes.command_for(recipes.RECIPES["python-fastapi"], "backend")
    assert argv[:5] == ("python", "-I", "-B", "-m", "pytest") and "--config-file=/opt/verify/pytest.ini" in argv and argv[-1] == "/workspace/backend"
    assert {"--rootdir=/workspace/backend", "--confcutdir=/workspace/backend", "--override-ini=pythonpath=/workspace/backend"} <= set(argv)
    assert all(a.split("=")[-1] == "/workspace/backend" for a in argv if "/workspace" in a)  # nothing outside the component


def test_react_runs_only_the_images_fixed_runner_and_installs_nothing() -> None:
    react = recipes.RECIPES["react-vite"]
    assert recipes.command_for(react, "frontend") == ("node", "/opt/verify/run-vitest.mjs", "/workspace/frontend")
    assert react.acceptance_job == ("react-acceptance", ("node", "/opt/verify/run-vitest.mjs", "{folder}", "--acceptance"))
    every = [a for r in recipes.RECIPES.values() for a in (*r.argv, *r.folder_args, *(r.acceptance_job[1] if r.acceptance_job else ()))]
    assert not {"npm", "npx", "yarn", "pnpm", "pip", "install", "sh", "bash"} & set(every)


def test_a_recipe_job_is_capped_by_the_settings_and_never_raises_them(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for stack in ("python-fastapi", "react-vite"):  # simulated approval of images that do not exist, inside this test only
        image = recipes.RECIPES[stack].runtime.image
        monkeypatch.setitem(policy.APPROVED_IMAGES, stack, policy.ApprovedImage(stack, image))
    settings = make_settings(tmp_path)
    jobs = {s: recipes.recipe_spec(r, None if not r.takes_folder else "web", tmp_path, settings) for s, r in recipes.RECIPES.items()}
    limits = {s: (j.memory, j.cpus, j.pids, j.timeout_seconds) for s, j in jobs.items()}
    assert limits == {"python-cli": ("512m", 1.0, 128, 120.0), "python-fastapi": ("1g", 1.0, 256, 180.0), "react-vite": ("1g", 1.0, 256, 300.0)}  # React asked for 2g, 2 CPUs, 512
    tight = make_settings(tmp_path, sandbox_memory="256m", sandbox_cpus=0.5, sandbox_pids_limit=64, sandbox_timeout_seconds=60)
    assert {(j.memory, j.cpus, j.pids, j.timeout_seconds) for j in (recipes.recipe_spec(r, None if not r.takes_folder else "web", tmp_path, tight) for r in recipes.RECIPES.values())} == {("256m", 0.5, 64, 60)}


def test_a_recipe_job_needs_an_approved_image(tmp_path: Path) -> None:
    for stack in ("python-fastapi", "react-vite"):
        with pytest.raises(policy.SandboxPolicyError, match="approved list"):
            recipes.recipe_spec(recipes.RECIPES[stack], "web", tmp_path, make_settings(tmp_path))


def test_a_runtime_is_ready_only_when_its_recipe_and_its_image_are_both_approved(monkeypatch: pytest.MonkeyPatch) -> None:
    for stack in ("python-fastapi", "react-vite"):
        recipe = recipes.RECIPES[stack]
        monkeypatch.setitem(recipes.RECIPES, stack, replace(recipe, runtime=replace(recipe.runtime, approved=True)))  # the flag alone
        ok, why = recipes.readiness(stack)
        assert not ok and why == f"the {recipe.name} recipe's runtime {recipe.runtime.image} is not on the sandbox's approved image list"
        monkeypatch.setitem(policy.APPROVED_IMAGES, stack, policy.ApprovedImage(stack, recipe.runtime.image))
        assert recipes.readiness(stack)[0]
