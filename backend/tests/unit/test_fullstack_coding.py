"""Component-aware coding and verification for a React + FastAPI request, end to end. **Simulated:** GPT is FakeLLM, the
coding provider writes scripted files, the repositories are fixtures, and the sandbox answers with fixture output.
The FastAPI and React runtimes do not exist; `approve_runtimes` pretends, inside these tests only, that they were
approved, to exercise the contract after that point. The product itself still blocks those stacks."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from app.coding.simulated import ScriptedAttempt, SimulatedLauncher
from app.db.models import Run, RunState
from app.demo import weather
from app.orchestrator.fake_llm import call
from app.repos.fixtures import OWNER, FixtureSource
from app.sandbox.fake import FakeSandbox
from app.sandbox.spec import ExecResult, ExecSpec, ExecStatus
from app.tools.adapters.repositories import RepositoryRuntime, register_repository_tools
from app.verification import acceptance, recipes
from tests.unit.test_coding_tools import Rig, finish, generate, pause, submit
from tests.unit.test_repository_tools import answer
from tests.unit.test_selection_feedback import adaptive

BACKEND = f"{OWNER}/fastapi-weather-api"
COMMIT = FixtureSource().detail(BACKEND).commit
PYTEST_PASS = "".join(f"tests/test_api.py::test_ac_00{n}_contract PASSED\n" for n in range(4, 10)) + "========== 6 passed in 0.31s ==========\n"
PYTEST_FAIL = "tests/test_api.py::test_ac_004_contract FAILED\n" + "".join(f"tests/test_api.py::test_ac_00{n}_contract PASSED\n" for n in range(5, 10)) + "AssertionError: 500 != 200\n========== 1 failed, 5 passed in 0.31s ==========\n"
VITEST_PASS = json.dumps({"numTotalTests": 3, "numPassedTests": 3, "numFailedTests": 0, "testResults": [{"assertionResults": [
    {"fullName": f"AC-00{n} dashboard", "status": "passed"} for n in range(1, 4)]}]})  # fmt: skip
ACCEPTANCE_PASS = "".join(f"test_at_00{n}_x (test_weather_acceptance.WeatherAcceptance.test_at_00{n}_x) ... ok\n" for n in range(1, 8)) + "\n----\nRan 7 tests in 0.01s\n\nOK\n"

BACKEND_FILES = {"backend/app/main.py": "from fastapi import FastAPI\napp = FastAPI()\n", "backend/app/weather.py": "def fetch_current(city):\n    raise NotImplementedError\n"}
FRONTEND_FILES = {"frontend/package.json": json.dumps({"dependencies": {"react": "^18.3.1", "react-dom": "^18.3.1"}, "devDependencies": {"vite": "^5", "vitest": "^2"}}),
                  "frontend/src/App.jsx": "const base = import.meta.env.VITE_API_BASE_URL;\nexport const url = `${base}/api/weather`;\n"}  # fmt: skip


@pytest.fixture
def approve_runtimes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulated approval of runtimes that do not exist. Only these tests see it."""
    for stack in ("python-fastapi", "react-vite"):
        recipe = recipes.RECIPES[stack]
        monkeypatch.setitem(recipes.RECIPES, stack, replace(recipe, runtime=replace(recipe.runtime, approved=True)))


class Sandbox:
    """Fixture answers by recipe. The back end's tests fail once, then pass."""

    def __init__(self) -> None:
        self.backend_runs = 0

    def __call__(self, spec: ExecSpec) -> ExecResult:
        if spec.command == "fastapi-pytest":
            self.backend_runs += 1
            output, status = (PYTEST_FAIL, ExecStatus.FAILED) if self.backend_runs == 1 else (PYTEST_PASS, ExecStatus.SUCCEEDED)
            return ExecResult(status, spec.command, spec.container_name, exit_code=0 if status is ExecStatus.SUCCEEDED else 1, stdout=output, cleanup="removed")
        if spec.command == "react-vitest":
            return ExecResult(ExecStatus.SUCCEEDED, spec.command, spec.container_name, exit_code=0, stdout=VITEST_PASS, cleanup="removed")
        return ExecResult(ExecStatus.SUCCEEDED, spec.command, spec.container_name, exit_code=0, stderr=ACCEPTANCE_PASS, cleanup="removed")


def opening() -> list[Any]:
    imported = call("import_repository", {"full_name": BACKEND, "commit": COMMIT, "component": "backend"})
    return [call("submit_analysis", weather.ANALYSIS), call("search_repositories", weather.BACKEND_SEARCH), call("search_repositories", weather.FRONTEND_SEARCH), imported,
            call("submit_plan", weather.FULLSTACK_PLAN), call("submit_criteria", weather.FULL_CRITERIA)]  # fmt: skip


def rig(tmp_path: Path, steps: list[Any], script: list[ScriptedAttempt], sandbox: Any) -> Rig:
    launcher = SimulatedLauncher(script)
    r = Rig(tmp_path, *steps, launcher=launcher, selector=adaptive(launcher), sandbox=FakeSandbox(sandbox))
    register_repository_tools(r.h.registry, RepositoryRuntime(FixtureSource()))
    text, sha = acceptance.freeze(weather.ACCEPTANCE_SUITE)
    with r.h.sessions.begin() as s:
        run = s.get(Run, r.run_id)
        run.acceptance_suite_json, run.acceptance_sha256 = text, sha
    r.advance()
    for _ in range(3):  # two searches and one import, each approved by a person
        answer(r.h, r.run_id)
    return r


def test_the_full_weather_request_is_coded_per_component_verified_per_recipe_repaired_and_delivered(tmp_path: Path, approve_runtimes: None) -> None:
    script = [ScriptedAttempt(BACKEND_FILES), ScriptedAttempt({"backend/tests/test_api.py": "def test_ac_004_contract():\n    pass\n"}), ScriptedAttempt(FRONTEND_FILES),
              ScriptedAttempt({"frontend/src/App.test.jsx": "test('AC-001 dashboard', () => {});\n"}), ScriptedAttempt({"backend/app/weather.py": "def fetch_current(city):\n    return {}\n"})]  # fmt: skip
    steps = [*opening(), generate(1), generate(2), generate(3), generate(4), submit(), generate(1, "Fix the 500."), submit("Fixed."), finish()]
    sandbox = Sandbox()
    r = rig(tmp_path, steps, script, sandbox)
    assert r.h.run(r.run_id).state is RunState.COMPLETED
    report = r.report()
    assert {d["component"]: d["action"] for d in report["repositories"]["decisions"]} == {"backend": "reuse", "frontend": "build"}
    assert [(a["task_order"], a["status"]) for a in report["coding_attempts"]] == [(1, "COMPLETED"), (2, "COMPLETED"), (3, "COMPLETED"), (4, "COMPLETED"), (1, "COMPLETED")]
    components = r.h.events_of(r.run_id, "verification.component")
    assert [(c["component"], c["recipe"], c["verdict"]) for c in components] == [("backend", "fastapi-pytest", "FAILED"), ("frontend", "react-vitest", "PASSED"),
                                                                                 ("backend", "fastapi-pytest", "PASSED"), ("frontend", "react-vitest", "PASSED")]  # fmt: skip
    assert [v["verdict"] for v in report["verifications"]] == ["FAILED", "PASSED"] and "backend: 1 failed" in report["verifications"][0]["reason"]
    assert [(d["category"], d["action"]) for d in report["recovery"]] == [("TEST_FAILURE", "REPAIR")]
    assert report["acceptance"]["verdict"] == "PASSED" and report["acceptance"]["passed"] == 7
    delivered = {f["path"] for f in report["delivered_files"]}
    assert {"backend/app/weather.py", "frontend/src/App.jsx", "frontend/src/App.test.jsx"} <= delivered
    assert {"backend/LICENSE", "backend/THIRD_PARTY_NOTICES.md"} <= set(r.h.workspace.list_files(r.project_id))  # imported before the copy, kept through delivery
    specs = r.sandbox.specs
    assert {s.argv[-1] for s in specs if s.command != "python-unittest"} == {"/workspace/backend", "/workspace/frontend"}
    assert {s.image for s in specs if s.command == "python-unittest"} == {"devpilot/verify-fastapi:1"}  # the suite runs in the Python component's runtime


def test_a_component_task_cannot_change_another_components_files(tmp_path: Path, approve_runtimes: None) -> None:
    escape = ScriptedAttempt({**BACKEND_FILES, "frontend/src/App.jsx": "stolen\n"})
    r = rig(tmp_path, [*opening(), generate(1), pause()], [escape], Sandbox())
    attempt = r.report()["coding_attempts"][0]
    assert attempt["status"] == "POLICY_VIOLATION" and any("outside this task's component folder backend/" in v for v in attempt["violations"])
    assert r.h.run(r.run_id).state is RunState.FAILED and r.report()["delivered_files"] == []


def test_a_dependency_the_runtime_does_not_provide_blocks_verification(tmp_path: Path, approve_runtimes: None) -> None:
    extra = {**FRONTEND_FILES, "frontend/package.json": json.dumps({"dependencies": {"react": "1", "left-pad": "1"}})}
    script = [ScriptedAttempt(BACKEND_FILES), ScriptedAttempt({"backend/tests/t.py": ""}), ScriptedAttempt(extra), ScriptedAttempt({"frontend/src/a.test.jsx": ""})]
    r = rig(tmp_path, [*opening(), generate(1), generate(2), generate(3), generate(4), submit(), pause()], script, Sandbox())
    run = r.h.run(r.run_id)
    assert run.state is RunState.STOPPED_BY_POLICY and "left-pad" in run.status_detail and r.sandbox.specs == []


def test_without_approved_runtimes_nothing_is_coded(tmp_path: Path) -> None:
    r = rig(tmp_path, [*opening(), generate(1), pause()], [ScriptedAttempt(BACKEND_FILES)], Sandbox())
    run = r.h.run(r.run_id)
    assert run.state is RunState.STOPPED_BY_POLICY and run.outcome == "verification_unavailable" and r.attempts() == []
