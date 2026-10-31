"""The offline end-to-end demonstration: `python -m app.demo.mvp`.

What is real here: the REST API, the orchestrator, the gates, the database, provider selection, the
attempt lifecycle, the audit, the disposable workspace, the Docker sandbox, the tests it runs, the
verdict, the recovery bounds, the delivery and the report.

What is simulated: GPT's replies (`FakeLLM`) and the coding provider (`SimulatedLauncher`), which writes
fixed files. **So this shows that the workflow works, not that any model can write code.** No OpenCode
server runs, and no model is called. Every report says so in its `provenance` block.

Nothing in the application knows these scenarios: they are data, here, and a run of any other
requirement takes the same path. The demo needs Docker and the image python:3.12-alpine.
"""

from __future__ import annotations

import argparse
import json
import secrets
import shutil
import sys
import tempfile
import time
import warnings
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.coding.audit import take_snapshot
from app.coding.candidate import tree_digest
from app.coding.simulated import PROVIDER_NAME, ScriptedAttempt, SimulatedLauncher
from app.coding.types import CodingStatus
from app.config import Settings
from app.main import create_app
from app.orchestrator.fake_llm import FakeLLM, Step, call
from app.sandbox.docker_cli import DockerCli
from app.sandbox.process import SubprocessRunner
from app.sandbox.runner import DockerSandbox
from app.selection.selector import ProviderOption, ProviderSelector
from app.verification.report import render_markdown

API = "/api/v1"
CALCULATOR_REQUIREMENT = "Create a Python calculator application supporting addition, subtraction, multiplication and division, with unit tests."
WORD_COUNT_REQUIREMENT = "Create a Python module that counts the words in a text, ignoring case, with unit tests."
_CAPABILITIES = frozenset({"code_generation", "test_generation", "documentation"})
_DEADLINE_SECONDS = 180
_SEED = {"README.md": "# The user's project\n\nThese notes were here before DevPilot ran.\n"}

_PY = {"language": "python", "framework": None, "multi_file": True, "size": "small"}

_CALCULATOR_ANALYSIS: dict[str, Any] = {
    "summary": "A small Python module for basic arithmetic on two numbers, with unit tests.",
    "requirements": [
        {"code": "R-001", "text": "Add, subtract, multiply and divide two numbers.", "kind": "functional"},
        {"code": "R-002", "text": "Dividing by zero raises a clear error.", "kind": "functional"},
        {"code": "R-003", "text": "The project includes automated unit tests.", "kind": "non_functional"},
    ],
    "assumptions": ["Operands are numbers.", "Only the standard library is available."],
    "ambiguities": [],
}
_CALCULATOR_PLAN: dict[str, Any] = {
    "stack": "python-cli",
    "stack_rationale": "A small library with no web interface fits the Python stack, and it can be tested in the sandbox.",
    "summary": "One module with the four operations, and a unittest file that proves each criterion.",
    "tasks": [
        {"title": "Arithmetic module", "description": "calculator.py with add, subtract, multiply and divide.", "capability": "code_generation",
         "requirement_refs": ["R-001", "R-002"], "features": _PY},
        {"title": "Unit tests", "description": "test_calculator.py with a test for each criterion.", "capability": "test_generation",
         "requirement_refs": ["R-001", "R-002", "R-003"], "features": _PY},
    ],
}  # fmt: skip


def _criterion(number: int, given: str, when: str, then: str, refs: list[str], priority: str = "MUST") -> dict[str, Any]:
    return {"code": f"AC-{number:03d}", "given": given, "when": when, "then": then, "priority": priority, "method": "unit", "requirement_refs": refs}


_CALCULATOR_CRITERIA: dict[str, Any] = {
    "criteria": [
        _criterion(1, "the calculator", "add(2, 3) is called", "it returns 5", ["R-001", "R-003"]),
        _criterion(2, "the calculator", "subtract(5, 2) is called", "it returns 3", ["R-001"]),
        _criterion(3, "the calculator", "multiply(4, 2.5) is called", "it returns 10.0", ["R-001"]),
        _criterion(4, "the calculator", "divide(9, 3) is called", "it returns 3.0", ["R-001"]),
        _criterion(5, "the calculator", "divide(1, 0) is called", "ZeroDivisionError is raised", ["R-002"]),
    ]
}

CALCULATOR = '''"""Basic arithmetic on two numbers."""


def add(a, b):
    return a + b


def subtract(a, b):
    return a - b


def multiply(a, b):
    return a * b


def divide(a, b):
    if b == 0:
        raise ZeroDivisionError("division by zero")
    return a / b
'''
BROKEN_CALCULATOR = CALCULATOR.replace("return a - b", "return b - a")  # a real bug: the operands are swapped
CALCULATOR_TESTS = '''import unittest

from calculator import add, divide, multiply, subtract


class CalculatorTests(unittest.TestCase):
    def test_ac_001_addition(self):
        self.assertEqual(add(2, 3), 5)

    def test_ac_002_subtraction(self):
        self.assertEqual(subtract(5, 2), 3)

    def test_ac_003_multiplication(self):
        self.assertEqual(multiply(4, 2.5), 10.0)

    def test_ac_004_division(self):
        self.assertEqual(divide(9, 3), 3.0)

    def test_ac_005_division_by_zero(self):
        with self.assertRaises(ZeroDivisionError):
            divide(1, 0)
'''

_WORD_ANALYSIS: dict[str, Any] = {
    "summary": "A Python module that counts how often each word appears in a text.",
    "requirements": [{"code": "R-001", "text": "Count words, ignoring case.", "kind": "functional"}],
    "assumptions": [],
    "ambiguities": [],
}
_WORD_PLAN: dict[str, Any] = {
    "stack": "python-cli",
    "stack_rationale": "A small text utility fits the Python stack.",
    "summary": "One module and its tests, in one task.",
    "tasks": [{"title": "Word counter", "description": "wordcount.py and test_wordcount.py.", "capability": "code_generation", "requirement_refs": ["R-001"], "features": _PY}],
}
_WORD_CRITERIA: dict[str, Any] = {"criteria": [_criterion(1, "a text", "count('A a b') is called", "a appears twice and b once", ["R-001"])]}
WORD_COUNT = 'from collections import Counter\n\n\ndef count(text):\n    return Counter(text.casefold().split())\n'
WORD_COUNT_TESTS = '''import unittest

from wordcount import count


class WordCountTests(unittest.TestCase):
    def test_ac_001_counts_ignoring_case(self):
        self.assertEqual(count("A a b"), {"a": 2, "b": 1})
'''


def _generate(task: int, instructions: str) -> Step:
    return call("generate_code", {"task_order": task, "instructions": instructions})


def _submit(summary: str = "Every task is done.") -> Step:
    return call("submit_implementation", {"summary": summary})


def _finish() -> Step:
    return call("finish", {"summary": "The project is built and its tests pass."})


def _calculator_opening() -> list[Step]:
    return [call("submit_analysis", _CALCULATOR_ANALYSIS), call("submit_plan", _CALCULATOR_PLAN), call("submit_criteria", _CALCULATOR_CRITERIA)]


@dataclass(frozen=True)
class Scenario:
    key: str
    title: str
    requirement: str
    gpt: list[Step]
    coding: list[ScriptedAttempt]
    expected_state: str
    max_repairs: int = 2
    settings: dict[str, Any] = field(default_factory=dict)


def scenarios() -> list[Scenario]:
    good = ScriptedAttempt({"calculator.py": CALCULATOR})
    tests = ScriptedAttempt({"test_calculator.py": CALCULATOR_TESTS})
    broken = ScriptedAttempt({"calculator.py": BROKEN_CALCULATOR})
    return [
        Scenario(
            "success", "Calculator, built and verified at the first try", CALCULATOR_REQUIREMENT,
            [*_calculator_opening(), _generate(1, "Write the module."), _generate(2, "Write the tests."), _submit(), _finish()],
            [good, tests], "COMPLETED",
        ),
        Scenario(
            "failed_tests", "A real bug that is never fixed: the failure is reported and the bound holds", CALCULATOR_REQUIREMENT,
            [*_calculator_opening(), _generate(1, "Write the module."), _generate(2, "Write the tests."), _submit(), _generate(1, "Fix subtract."), _submit("The fix is in.")],
            [broken, tests, broken], "FAILED", max_repairs=1,
        ),
        Scenario(
            "coding_failure", "A simulated coding failure: undone, retried, then verified", CALCULATOR_REQUIREMENT,
            [*_calculator_opening(), _generate(1, "Write the module."), _generate(1, "Try again."), _generate(2, "Write the tests."), _submit(), _finish()],
            [ScriptedAttempt({"half_written.py": "def half("}, CodingStatus.FAILED, "simulated: the engine stopped half way"), good, tests], "COMPLETED",
        ),
        Scenario(
            "repair", "A real bug, repaired after the sandbox caught it", CALCULATOR_REQUIREMENT,
            [*_calculator_opening(), _generate(1, "Write the module."), _generate(2, "Write the tests."), _submit(), _generate(1, "Fix subtract."), _submit("The fix is in."), _finish()],
            [broken, tests, good], "COMPLETED",
        ),
        Scenario(
            "policy_violation", "An attempt that touches a protected file: refused by the audit, and the run stops", CALCULATOR_REQUIREMENT,
            [*_calculator_opening(), _generate(1, "Write the module.")],
            [ScriptedAttempt({"calculator.py": CALCULATOR, "opencode.json": '{"permission": {"*": "allow"}}'})], "FAILED",
        ),
        Scenario(
            "other_software", "A different requirement through the same path", WORD_COUNT_REQUIREMENT,
            [call("submit_analysis", _WORD_ANALYSIS), call("submit_plan", _WORD_PLAN), call("submit_criteria", _WORD_CRITERIA), _generate(1, "Write it."), _submit(), _finish()],
            [ScriptedAttempt({"wordcount.py": WORD_COUNT, "test_wordcount.py": WORD_COUNT_TESTS})], "COMPLETED",
        ),
    ]  # fmt: skip


@dataclass
class ScenarioResult:
    key: str
    title: str
    expected_state: str
    state: str
    outcome: str | None
    report: dict[str, Any]
    original_before: dict[str, str]
    original_after: dict[str, str]
    seeded_file_intact: bool
    left_in_work_dir: list[str]
    gpt_steps_left: int
    coding_steps_left: int

    @property
    def original_unchanged(self) -> bool:
        return self.original_before == self.original_after

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "original_unchanged": self.original_unchanged}


def run_scenario(scenario: Scenario, data_dir: Path, sandbox_factory: Callable[[Settings], Any] = DockerSandbox.from_settings) -> ScenarioResult:
    """One requirement through the real API, with GPT and the coding provider scripted."""
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=".*httpx.*")
        from fastapi.testclient import TestClient

    data_dir.mkdir(parents=True)
    settings = Settings(
        _env_file=None,  # never read the real .env: no real key is involved
        devpilot_api_token=secrets.token_urlsafe(32),
        database_url=f"sqlite:///{(data_dir / 'demo.db').as_posix()}",
        workspaces_dir=data_dir / "workspaces",
        coding_checkpoints_dir=data_dir / "checkpoints",
        coding_work_dir=data_dir / "work",
        max_repair_attempts=scenario.max_repairs,
        log_level="WARNING",
        **scenario.settings,
    )
    llm = FakeLLM(*scenario.gpt)
    launcher = SimulatedLauncher(scenario.coding)
    selector = ProviderSelector([ProviderOption(PROVIDER_NAME, launcher, _CAPABILITIES, simulated=True)])
    app = create_app(settings, llm=llm, sandbox=sandbox_factory(settings), coding=selector)
    headers = {"Authorization": f"Bearer {settings.devpilot_api_token.get_secret_value()}"}
    with TestClient(app) as client:
        project = client.post(f"{API}/projects", json={"name": scenario.title[:60], "requirement_text": scenario.requirement}, headers=headers).json()
        for rel, content in _SEED.items():
            app.state.workspace_fs.write_text(project["id"], rel, content)
        original = app.state.workspace_fs.project_dir(project["id"])
        before = _hashes(original)
        run = client.post(f"{API}/projects/{project['id']}/runs", headers=headers).json()
        client.post(f"{API}/runs/{run['id']}/start", headers=headers).raise_for_status()
        deadline = time.monotonic() + _DEADLINE_SECONDS
        while time.monotonic() < deadline:
            run = client.get(f"{API}/runs/{run['id']}", headers=headers).json()
            if run["ended_at"] or run["pause_reason"]:
                break
            time.sleep(0.1)
        answer = client.get(f"{API}/runs/{run['id']}/report", headers=headers)
        report = answer.json() if answer.status_code == 200 else {"missing": True, "status_detail": run["status_detail"]}
    after = _hashes(original)
    seed_intact = all((original / rel).read_text(encoding="utf-8") == content for rel, content in _SEED.items())
    left = sorted(p.name for p in settings.coding_work_dir.iterdir()) if settings.coding_work_dir.is_dir() else []
    return ScenarioResult(
        key=scenario.key, title=scenario.title, expected_state=scenario.expected_state, state=run["state"], outcome=run["outcome"], report=report,
        original_before=before, original_after=after, seeded_file_intact=seed_intact, left_in_work_dir=left,
        gpt_steps_left=llm.remaining, coding_steps_left=len(launcher._script),
    )  # fmt: skip


def _hashes(root: Path) -> dict[str, str]:
    snapshot = take_snapshot(root)
    return {"tree": tree_digest(snapshot), **{rel: state.sha256 for rel, state in snapshot.files.items()}}


def _describe(result: ScenarioResult, emit: Callable[[str], None]) -> None:
    report = result.report
    emit(f"\n=== {result.key}: {result.title}")
    emit(f"Final state: {result.state} (expected {result.expected_state}), outcome {result.outcome}")
    for attempt in report.get("coding_attempts", []):
        emit(f"  coding task {attempt['task_order']} [{attempt['provider']}]: {attempt['status']}, kept={attempt['kept']}, restored={attempt['restored']}, phase {attempt['phase']}")
        for violation in attempt.get("violations") or []:
            emit(f"    audit refused: {violation}")
    for number, check in enumerate(report.get("verifications", []), start=1):
        emit(f"  Docker test run {number}: {check['verdict']}, {check['passed']} passed of {check['ran']}, exit code {check['exit_code']}, container {check['container']} ({check['cleanup']})")
        for name in check["failed_names"]:
            emit(f"    failed: {name}")
    for decision in report.get("recovery", []):
        emit(f"  recovery: {decision['failure_class']} -> {decision['action']} ({decision['attempts_used']} of {decision['attempts_allowed']} used). {decision['reason']}")
    emit(f"  delivered: {[f['path'] for f in report.get('delivered_files', [])] or 'nothing'}")
    change = "unchanged" if result.original_unchanged else "changed, by the delivery" if report.get("delivered_files") else "CHANGED WITHOUT A DELIVERY"
    emit(f"  original workspace: {change}; the file that was already there is {'intact' if result.seeded_file_intact else 'DAMAGED'}")
    emit(f"  simulated in this run: {', '.join(report.get('provenance', {}).get('simulated', []))}")


def main() -> int:
    parser = argparse.ArgumentParser(description="DevPilot's offline end-to-end demonstration, with real Docker tests")
    parser.add_argument("--out", type=Path, help="write the reports here, as JSON and Markdown")
    parser.add_argument("--only", help="run one scenario by its key")
    args = parser.parse_args()
    cli = DockerCli("docker", SubprocessRunner())
    print(f"Docker server {cli.server_version()}. GPT and the coding provider are simulated in every scenario below.")
    root = Path(tempfile.mkdtemp(prefix="devpilot-mvp-"))
    results: list[ScenarioResult] = []
    try:
        for scenario in scenarios():
            if args.only and args.only != scenario.key:
                continue
            result = run_scenario(scenario, root / scenario.key)
            results.append(result)
            _describe(result, print)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    if args.out is not None:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "mvp-demo.json").write_text(json.dumps([r.to_dict() for r in results], indent=2), encoding="utf-8")
        (args.out / "mvp-demo.md").write_text("\n\n".join(render_markdown(r.report) for r in results if "run" in r.report), encoding="utf-8")
    matched = sum(r.state == r.expected_state for r in results)
    print(f"\n{matched} of {len(results)} scenarios ended as expected. Containers left: {len(cli.labelled_containers())}")
    return 0 if matched == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
