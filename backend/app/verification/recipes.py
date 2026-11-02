"""Verification recipes: how each stack's tests run, fixed in code. GPT and repository READMEs never supply a command.

A recipe names an exact argument list, the folder it runs in (the plan's component folder, checked here), a runtime
image with no network, the packages that image provides, resource limits, the test output format and its parser, the
failure conditions, and how the evaluator's acceptance suite runs. Nothing is installed when a recipe runs: a project
that needs a package the image does not provide fails before running, with the missing names.

**The FastAPI and React runtimes are defined, not built.** Their images do not exist yet and have never run, so their
recipes are not approved and every project that needs them is blocked, with this reason, until a person approves a
built image. Only `python-cli` (python:3.12-alpine, the approved sandbox image) is approved.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass

from app.sandbox.policy import APPROVED_COMMANDS
from app.sandbox.spec import WORKSPACE_TARGET, ExecResult, ExecStatus
from app.verification.results import TestCounts, Verdict, VerificationResult, parse_unittest

UNITTEST, PYTEST, VITEST_JSON = "unittest-verbose", "pytest-verbose", "vitest-json"
FOLDER = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


class RecipeError(ValueError):
    pass


@dataclass(frozen=True)
class Runtime:
    image: str
    approved: bool  # True only for an image that exists and has been verified in the sandbox
    provides: tuple[str, ...]  # the packages baked into the image; nothing is installed at run time
    memory: str
    cpus: float
    pids: int
    timeout_seconds: float
    network: str = "none"


@dataclass(frozen=True)
class Recipe:
    name: str
    stack: str
    argv: tuple[str, ...]  # exact; the component folder is appended by `command_for`, after it is checked
    takes_folder: bool
    result_format: str
    manifest: str | None  # the file whose dependencies must all be provided by the runtime
    runtime: Runtime
    failure_conditions: tuple[str, ...]
    acceptance: str  # how the evaluator's frozen suite runs for this stack


_COMMON_FAILURES = ("a non-zero exit code", "no test ran", "output the parser cannot read", "the time, memory, output or process limit", "a dependency the runtime does not provide")
RECIPES: dict[str, Recipe] = {
    "python-cli": Recipe(
        "python-unittest", "python-cli", APPROVED_COMMANDS["python-unittest"].argv, False, UNITTEST, None,
        Runtime("python:3.12-alpine", True, ("python standard library",), "512m", 1.0, 128, 120.0), _COMMON_FAILURES,
        "the same command, in a separate folder holding the candidate without its own tests and the evaluator's files",
    ),
    "python-fastapi": Recipe(
        "fastapi-pytest", "python-fastapi", ("python", "-I", "-B", "-m", "pytest", "-v", "-p", "no:cacheprovider", "--color=no"), True, PYTEST, "requirements.txt",
        Runtime("devpilot/verify-fastapi:1", False, ("fastapi", "starlette", "pydantic", "httpx", "anyio", "uvicorn", "pytest", "python-dotenv"), "1g", 1.0, 256, 180.0),
        _COMMON_FAILURES, "python -I -B -m unittest in the same image, so the evaluator's tests can import FastAPI's TestClient",
    ),
    "react-vite": Recipe(
        "react-vitest", "react-vite", ("node", "/opt/verify/node_modules/vitest/vitest.mjs", "run", "--reporter=json", "--environment=jsdom", "--root"), True, VITEST_JSON, "package.json",
        Runtime("devpilot/verify-react:1", False, ("react", "react-dom", "vite", "vitest", "jsdom", "@vitejs/plugin-react", "@testing-library/react", "@testing-library/jest-dom",
                                                   "@testing-library/user-event", "typescript"), "2g", 2.0, 512, 300.0),
        _COMMON_FAILURES, "front-end criteria are proved by the component's Vitest run; the Python acceptance suite runs in the FastAPI image",
    ),
}  # fmt: skip


def recipe_for(stack: str) -> Recipe | None:
    return RECIPES.get(stack)


def readiness(stack: str) -> tuple[bool, str]:
    """Whether this stack's work can be verified now, and if not, exactly why."""
    recipe = RECIPES.get(stack)
    if recipe is None:
        return False, f"no verification recipe exists for stack {stack}"
    if not recipe.runtime.approved:
        return False, f"the {recipe.name} recipe needs the {recipe.runtime.image} runtime, which is defined but not built or approved"
    return True, f"{recipe.name} runs in {recipe.runtime.image}"


def command_for(recipe: Recipe, folder: str | None) -> tuple[str, ...]:
    """The exact argument list. The folder must be one plain level, as the plan gate allows."""
    if not recipe.takes_folder:
        return recipe.argv
    if folder is None or not FOLDER.fullmatch(folder):
        raise RecipeError(f"{recipe.name} runs in a component folder named like 'backend'; {folder!r} is not one")
    return (*recipe.argv, f"{WORKSPACE_TARGET}/{folder}")


def dependency_problems(recipe: Recipe, files: Mapping[str, bytes], folder: str | None) -> list[str]:
    """Dependencies the project declares that the runtime does not provide. Read only; nothing is installed."""
    if recipe.manifest is None:
        return []
    rel = f"{folder}/{recipe.manifest}" if folder else recipe.manifest
    data = files.get(rel)
    if data is None:
        return [f"{rel} is missing"]
    text = data.decode("utf-8", errors="replace")
    if recipe.manifest == "package.json":
        try:
            manifest = json.loads(text)
        except ValueError:
            return [f"{rel} is not valid JSON"]
        names = {*(manifest.get("dependencies") or {}), *(manifest.get("devDependencies") or {})}
    elif recipe.manifest == "requirements.txt":
        names = {re.split(r"[\s<>=!~;\[]", line.strip(), maxsplit=1)[0].lower() for line in text.splitlines() if line.strip() and not line.lstrip().startswith(("#", "-"))}
    else:
        names = set(tomllib.loads(text).get("project", {}).get("dependencies", []))
    missing = sorted(n for n in names if n.lower() not in {p.lower() for p in recipe.runtime.provides})
    return [f"{rel} needs {', '.join(missing)}, which {recipe.runtime.image} does not provide"] if missing else []


_PYTEST_CASE = re.compile(r"^(?P<file>[^\s:]+\.py)::(?:(?P<cls>\w+)::)?(?P<name>\w+)(?:\[[^\]]*\])? (?P<outcome>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)", re.MULTILINE)
_PYTEST_SUMMARY = re.compile(r"^=+ (?P<body>.+?) in [\d.]+s(?: \([^)]*\))? =+$", re.MULTILINE)


def parse_pytest(output: str) -> TestCounts:
    summary = list(_PYTEST_SUMMARY.finditer(output))
    if not summary:
        return TestCounts(readable=False)
    counts = {kind: int(n) for n, kind in re.findall(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed)", summary[-1]["body"])}
    passed, failed = [], []
    for case in _PYTEST_CASE.finditer(output):
        (passed if case["outcome"] in ("PASSED", "XFAIL") else failed if case["outcome"] in ("FAILED", "ERROR", "XPASS") else []).append(case["name"])
    errors = counts.get("error", 0) + counts.get("errors", 0)
    ran = counts.get("passed", 0) + counts.get("failed", 0) + errors + counts.get("skipped", 0) + counts.get("xfailed", 0) + counts.get("xpassed", 0)
    return TestCounts(True, ran, counts.get("failed", 0) + counts.get("xpassed", 0), errors, counts.get("skipped", 0), tuple(passed), tuple(failed), summary[-1]["body"])


def _name(title: str) -> str:
    """'AT-001 renders the city search' becomes 'at_001_renders_the_city_search', so criteria map as for Python tests."""
    return re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")


def parse_vitest_json(output: str) -> TestCounts:
    start = output.find("{")
    try:
        data = json.loads(output[start:]) if start >= 0 else None
    except ValueError:
        data = None
    if not isinstance(data, dict) or "numTotalTests" not in data:
        return TestCounts(readable=False)
    passed, failed = [], []
    for suite in data.get("testResults", []):
        for case in suite.get("assertionResults", []):
            (passed if case.get("status") == "passed" else failed if case.get("status") == "failed" else []).append(_name(case.get("fullName") or case.get("title", "")))
    return TestCounts(True, int(data["numTotalTests"]), int(data.get("numFailedTests", 0)), 0, int(data.get("numPendingTests", 0)), tuple(passed), tuple(failed),
                      f"{data.get('numPassedTests', 0)} passed, {data.get('numFailedTests', 0)} failed")  # fmt: skip


def judge_recipe(recipe: Recipe, result: ExecResult) -> VerificationResult:
    """One component's run, judged by its recipe's parser. Every way of not knowing is NOT_RUN or FAILED, never PASSED."""
    tail = (result.stdout + result.stderr)[-4_000:]
    base = {"exec_status": result.status.value, "exit_code": result.exit_code, "output_tail": tail, "container": result.container, "duration_ms": result.duration_ms, "cleanup": result.cleanup}
    if result.status in (ExecStatus.UNAVAILABLE, ExecStatus.ERROR, ExecStatus.CANCELLED):
        return VerificationResult(Verdict.NOT_RUN, result.detail or f"the sandbox ended with {result.status.value}", counts=TestCounts(readable=False), criteria=(), **base)
    counts = parse(recipe, result.stdout, result.stderr)
    reason = None
    if result.status not in (ExecStatus.SUCCEEDED, ExecStatus.FAILED):
        reason = result.detail or f"the tests ended with {result.status.value}"
    elif not counts.readable:
        reason = f"the {recipe.name} output could not be read"
    elif counts.ran == 0:
        reason = "no tests ran"
    elif result.status is ExecStatus.FAILED or result.exit_code != 0 or counts.failures or counts.errors:
        reason = f"{counts.failures} failed and {counts.errors} errored of {counts.ran} tests"
    return VerificationResult(Verdict.PASSED if reason is None else Verdict.FAILED, reason or "every test passed", counts=counts, criteria=(), **base)


def parse(recipe: Recipe, stdout: str, stderr: str) -> TestCounts:
    if recipe.result_format == UNITTEST:
        return parse_unittest(stderr)
    if recipe.result_format == PYTEST:
        return parse_pytest(stdout)
    return parse_vitest_json(stdout)
