"""The first real-world request: a React + FastAPI weather application. **Specification and scripted data only.**

Nothing in the application is specific to weather: this module is data for one request. It holds the request, the
analysis and plan GPT is scripted to give in offline tests, and the frozen acceptance suite an evaluator would attach.
The suite needs FastAPI in the sandbox to run, which the sandbox does not have yet, so it is checked by parsing only.
No weather service is called and no weather data is invented: the tests replace the provider with fixed values.

Two scripted scopes show where the request stops today. With the full request, the plan gate refuses a plan, because
a plan names one stack and React and FastAPI are two (`react-vite`, `python-fastapi`): the React requirements cannot be
covered. With a back-end-only scope, the plan is accepted, and the run then stops because no tool can build
`python-fastapi` and it has no approved test command. Both gaps are recorded in docs/MILESTONE_1_GITHUB.md.
"""

from __future__ import annotations

from typing import Any

from app.orchestrator.fake_llm import Step, call
from app.verification.acceptance import AcceptanceSuite, SuiteCriterion

REQUIREMENT = (
    "Build a weather application using React and FastAPI. Search existing public GitHub repositories, evaluate suitable open-source implementations, "
    "reuse compatible code, add the missing features, run tests, repair failures, and deliver the final application."
)
SEARCH = {"query": "weather dashboard react fastapi city search", "must_have": ["react", "fastapi"]}

ANALYSIS: dict[str, Any] = {
    "summary": "A weather dashboard: a React front end with city search, and a FastAPI back end that calls an approved weather provider.",
    "requirements": [
        {"code": "R-001", "text": "A responsive React dashboard with a city search.", "kind": "functional"},
        {"code": "R-002", "text": "Show the current temperature, humidity and wind speed for the chosen city.", "kind": "functional"},
        {"code": "R-003", "text": "Show a loading state while waiting and a clear error state on failure.", "kind": "functional"},
        {"code": "R-004", "text": "A FastAPI endpoint returns the current weather for a validated city name.", "kind": "functional"},
        {"code": "R-005", "text": "An unknown city and a failing weather provider each give a clear, distinct error.", "kind": "functional"},
        {"code": "R-006", "text": "The weather provider's endpoint is configurable, not hard-coded.", "kind": "non_functional"},
        {"code": "R-007", "text": "Reuse a suitable, permissively licensed public repository when one exists, with attribution.", "kind": "non_functional"},
        {"code": "R-008", "text": "Automated tests cover the API contract, invalid input, provider failure and front-end integration.", "kind": "non_functional"},
    ],
    "assumptions": ["The weather provider is chosen and approved before a live run.", "Tests never call a real weather service."],
    "ambiguities": ["Which weather provider, and whether it needs an API key."],
}

# The back-end-only scope: the full request's R-004 to R-008, numbered from R-001 as the gate requires.
BACKEND_ANALYSIS: dict[str, Any] = {
    **ANALYSIS,
    "summary": "The back end of the weather dashboard only: a FastAPI service that calls an approved weather provider.",
    "requirements": [{**r, "code": f"R-{n:03d}"} for n, r in enumerate(ANALYSIS["requirements"][3:], start=1)],
}

_PY = {"language": "python", "framework": "fastapi", "multi_file": True, "size": "small"}
BACKEND_PLAN: dict[str, Any] = {
    "stack": "python-fastapi",
    "stack_rationale": "The back end is a FastAPI service. The React front end is a second stack, which a single plan cannot name yet.",
    "summary": "Adapt the imported back end to the weather contract, then its tests.",
    "tasks": [
        {"title": "Weather API", "description": "Adapt backend/app to serve GET /api/weather?city= with validation, provider errors and a configurable provider URL.",
         "capability": "code_generation", "requirement_refs": ["R-001", "R-002", "R-003", "R-004"], "features": _PY},
        {"title": "API tests", "description": "Tests for the contract, invalid cities and provider failures, with the provider replaced.",
         "capability": "test_generation", "requirement_refs": ["R-002", "R-005"], "features": _PY},
    ],
}  # fmt: skip
# The same tasks against the full request's numbering: the React requirements, R-001 to R-003, stay uncovered.
PLAN: dict[str, Any] = {
    **BACKEND_PLAN,
    "tasks": [{**task, "requirement_refs": [f"R-{int(ref[2:]) + 3:03d}" for ref in task["requirement_refs"]]} for task in BACKEND_PLAN["tasks"]],
}


def _criterion(number: int, given: str, when: str, then: str, refs: list[str], method: str = "api") -> dict[str, Any]:
    return {"code": f"AC-{number:03d}", "given": given, "when": when, "then": then, "priority": "MUST", "method": method, "requirement_refs": refs}


CRITERIA: dict[str, Any] = {
    "criteria": [
        _criterion(1, "a known city", "GET /api/weather?city= is called", "it returns temperature, humidity and wind speed", ["R-001"]),
        _criterion(2, "an empty or malformed city", "the endpoint is called", "it answers 422 without calling the provider", ["R-001"]),
        _criterion(3, "a city the provider does not know", "the endpoint is called", "it answers 404", ["R-002"]),
        _criterion(4, "a failing provider", "the endpoint is called", "it answers 502 without internal details", ["R-002"]),
        _criterion(5, "WEATHER_API_BASE_URL is set", "the back end starts", "it calls that provider URL", ["R-003"], "unit"),
        _criterion(6, "code reused from a public repository", "the project is delivered", "its licence file and THIRD_PARTY_NOTICES.md are delivered with it", ["R-004"], "static"),
        _criterion(7, "the project's own tests", "they run", "they cover the contract, invalid cities and provider failures", ["R-005"], "unit"),
    ]
}

ACCEPTANCE_TESTS = '''"""Acceptance tests for the weather back end, written by the evaluator before any code exists.

Contract: backend/app/main.py exposes `app` (FastAPI). backend/app/weather.py holds `fetch_current(city)`, the only
code that calls the provider, raising `CityNotFound` or `ProviderError`, and `provider_base_url()`, read from
WEATHER_API_BASE_URL. GET /api/weather?city=<name> answers {"city", "temperature_c", "humidity_pct",
"wind_speed_ms", "description"}. No test calls a real weather service: the provider is replaced by fixed values.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "backend"))
os.environ.setdefault("WEATHER_API_BASE_URL", "https://weather.invalid")

from fastapi.testclient import TestClient  # noqa: E402

from app import weather  # noqa: E402
from app.main import app  # noqa: E402

READING = {"city": "Hyderabad", "temperature_c": 31.5, "humidity_pct": 62, "wind_speed_ms": 3.4, "description": "scattered clouds"}


class WeatherAcceptance(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_at_001_returns_the_current_weather_for_a_city(self):
        with mock.patch.object(weather, "fetch_current", return_value=READING):
            answer = self.client.get("/api/weather", params={"city": "Hyderabad"})
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(set(answer.json()), set(READING))

    def test_at_002_reports_temperature_humidity_and_wind_as_numbers(self):
        with mock.patch.object(weather, "fetch_current", return_value=READING):
            body = self.client.get("/api/weather", params={"city": "Hyderabad"}).json()
        for key in ("temperature_c", "humidity_pct", "wind_speed_ms"):
            self.assertIsInstance(body[key], (int, float))

    def test_at_003_refuses_an_invalid_city_without_calling_the_provider(self):
        with mock.patch.object(weather, "fetch_current") as fetch:
            for bad in ("", "   ", "x" * 101, "<script>"):
                self.assertEqual(self.client.get("/api/weather", params={"city": bad}).status_code, 422, bad)
        fetch.assert_not_called()

    def test_at_004_an_unknown_city_is_a_404(self):
        with mock.patch.object(weather, "fetch_current", side_effect=weather.CityNotFound("Atlantis")):
            self.assertEqual(self.client.get("/api/weather", params={"city": "Atlantis"}).status_code, 404)

    def test_at_005_a_provider_failure_is_a_502_with_no_internal_detail(self):
        with mock.patch.object(weather, "fetch_current", side_effect=weather.ProviderError("upstream timed out")):
            answer = self.client.get("/api/weather", params={"city": "Hyderabad"})
        self.assertEqual(answer.status_code, 502)
        self.assertNotIn("Traceback", answer.text)

    def test_at_006_the_provider_endpoint_is_configurable(self):
        with mock.patch.dict(os.environ, {"WEATHER_API_BASE_URL": "https://provider.example/v1"}):
            self.assertEqual(weather.provider_base_url(), "https://provider.example/v1")

    def test_at_007_the_front_end_calls_the_back_end_through_a_configurable_url(self):
        sources = "".join(p.read_text(encoding="utf-8") for p in (ROOT / "frontend" / "src").rglob("*") if p.suffix in (".js", ".jsx", ".ts", ".tsx"))
        self.assertIn("VITE_API_BASE_URL", sources)
        self.assertIn("/api/weather", sources)
'''

ACCEPTANCE_SUITE = AcceptanceSuite(
    criteria=(
        SuiteCriterion("AT-001", "GET /api/weather?city= returns the current weather in the agreed shape"),
        SuiteCriterion("AT-002", "temperature, humidity and wind speed are numbers"),
        SuiteCriterion("AT-003", "an empty, overlong or malformed city is refused with 422 and the provider is not called"),
        SuiteCriterion("AT-004", "a city the provider does not know is a 404"),
        SuiteCriterion("AT-005", "a provider failure is a 502 that exposes no internal detail"),
        SuiteCriterion("AT-006", "the provider's base URL comes from WEATHER_API_BASE_URL"),
        SuiteCriterion("AT-007", "the front end calls the back end through VITE_API_BASE_URL and /api/weather (a static check of the source)", "SHOULD"),
    ),
    files={"test_weather_acceptance.py": ACCEPTANCE_TESTS},
)

# Criteria no Python test can prove yet: they need a Node test image (Vitest, Playwright) and an approved provider.
PENDING_CRITERIA = (
    "the dashboard is usable at phone and desktop widths",
    "a loading state shows while a request is in flight",
    "an error state shows when the back end answers with an error",
    "the provider's raw response is parsed correctly (written once the provider is chosen)",
)


_TS = {"language": "typescript", "framework": "react", "multi_file": True, "size": "small"}
COMPONENTS = [{"name": "backend", "stack": "python-fastapi", "path": "backend"}, {"name": "frontend", "stack": "react-vite", "path": "frontend"}]
FULLSTACK_PLAN: dict[str, Any] = {
    "stack": "python-fastapi",
    "stack_rationale": "Two components: a FastAPI back end and a React front end, each with its own stack and folder.",
    "summary": "Adapt the reused back end, build the front end from scratch, and test both.",
    "components": COMPONENTS,
    "tasks": [
        {"title": "Weather API", "description": "Adapt backend/ to serve GET /api/weather?city= with validation, provider errors and a configurable provider URL.",
         "capability": "code_generation", "requirement_refs": ["R-004", "R-005", "R-006", "R-007"], "features": _PY, "component": "backend", "depends_on": []},
        {"title": "API tests", "description": "Tests for the contract, invalid cities and provider failures, with the provider replaced.",
         "capability": "test_generation", "requirement_refs": ["R-005", "R-008"], "features": _PY, "component": "backend", "depends_on": [1]},
        {"title": "Weather dashboard", "description": "A responsive React dashboard in frontend/ with city search, loading and error states, calling /api/weather through VITE_API_BASE_URL.",
         "capability": "code_generation", "requirement_refs": ["R-001", "R-002", "R-003"], "features": _TS, "component": "frontend", "depends_on": [1]},
        {"title": "Dashboard tests", "description": "Vitest tests for the search, the reading, the loading state and the error state, with the API replaced.",
         "capability": "test_generation", "requirement_refs": ["R-003", "R-008"], "features": _TS, "component": "frontend", "depends_on": [3]},
    ],
}  # fmt: skip
FULL_CRITERIA: dict[str, Any] = {
    "criteria": [
        _criterion(1, "the dashboard at phone and desktop widths", "a city is searched", "its weather is shown", ["R-001"], "e2e"),
        _criterion(2, "a reading from the back end", "the dashboard shows it", "temperature, humidity and wind speed are visible", ["R-002"], "e2e"),
        _criterion(3, "a slow or failing back end", "a city is searched", "a loading state, then an error state, is shown", ["R-003"], "e2e"),
        _criterion(4, "a known city", "GET /api/weather?city= is called", "it returns the agreed fields", ["R-004"]),
        _criterion(5, "an empty or malformed city", "the endpoint is called", "it answers 422 without calling the provider", ["R-004"]),
        _criterion(6, "an unknown city or a failing provider", "the endpoint is called", "it answers 404 or 502 without internal details", ["R-005"]),
        _criterion(7, "WEATHER_API_BASE_URL is set", "the back end starts", "it calls that provider URL", ["R-006"], "unit"),
        _criterion(8, "code reused from a public repository", "the project is delivered", "its licence file and THIRD_PARTY_NOTICES.md are delivered with it", ["R-007"], "static"),
        _criterion(9, "the project's own tests", "they run", "they cover the contract, the errors, the search and the states", ["R-008"], "unit"),
    ]
}
BACKEND_SEARCH = {"query": "weather api fastapi backend", "must_have": ["fastapi"], "component": "backend"}
FRONTEND_SEARCH = {"query": "react weather frontend", "must_have": ["react", "typescript"], "component": "frontend"}


def fullstack_steps(backend: str, commit: str) -> list[Step]:
    """The full-stack rehearsal: one search per component, an import that a person first denies and then approves,
    a two-component plan with dependent tasks, criteria for every requirement, and the first coding step."""
    again = call("import_repository", {"full_name": backend, "commit": commit, "component": "backend"})
    return [
        call("submit_analysis", ANALYSIS), call("search_repositories", BACKEND_SEARCH), call("search_repositories", FRONTEND_SEARCH),
        call("import_repository", {"full_name": backend, "commit": commit, "component": "backend"}), again,
        call("submit_plan", FULLSTACK_PLAN), call("submit_criteria", FULL_CRITERIA), call("generate_code", {"task_order": 1, "instructions": "Adapt the reused back end."}),
    ]  # fmt: skip


def offline_steps(full_name: str, commit: str, *, scope: str = "full") -> list[Step]:
    """What GPT is scripted to do offline: analyse, search, import what Python decided to reuse, plan, set criteria, try
    to code. `scope="backend"` analyses the back end only, to show what happens after planning."""
    analysis, plan = (ANALYSIS, PLAN) if scope == "full" else (BACKEND_ANALYSIS, BACKEND_PLAN)
    return [
        call("submit_analysis", analysis), call("search_repositories", SEARCH), call("import_repository", {"full_name": full_name, "commit": commit}),
        call("submit_plan", plan), call("submit_criteria", CRITERIA), call("generate_code", {"task_order": 1, "instructions": "Adapt the imported back end to the weather contract."}),
    ]  # fmt: skip
