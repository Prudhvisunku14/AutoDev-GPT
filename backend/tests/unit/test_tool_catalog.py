"""The tool catalog: reviewed facts joined with live readiness, and comparisons that never invent a capability."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import REPO_ROOT
from app.core.egress import EgressPolicy
from app.db.models import RunState
from app.demo import rag
from app.demo.competition import _simulated
from app.drive import fixtures as drive_fixtures
from app.main import create_app
from app.n8n.fixtures import FakeN8n
from app.n8n.runtime import build_n8n_runtime
from app.orchestrator.fake_llm import FakeLLM
from app.repos.fixtures import FixtureSource
from app.sandbox.replay import ReplaySandbox
from app.selection.catalog import build_selector
from app.toolcatalog.catalog import ToolCatalog
from app.toolcatalog.live import Readiness
from app.toolcatalog.model import CatalogError, load_facts
from app.tools.adapters.catalog import CompareToolsArgs
from app.tools.registry import strict_json_schema
from tests.helpers import ALLOWLIST_FILE, API_TOKEN, make_settings

SHIPPED = REPO_ROOT / "config" / "tool_catalog.yaml"
HEADERS = {"Authorization": f"Bearer {API_TOKEN}"}
EGRESS = EgressPolicy.from_file(ALLOWLIST_FILE)
N8N = {"n8n_enabled": True, "n8n_callback_url": "http://127.0.0.1:8000/integrations/n8n/callback", "n8n_webhook_secret": "test-n8n-secret-" + "s" * 32, "n8n_api_key": "test-n8n-api-key-" + "k" * 24}


def everything_app(tmp_path: Path) -> Any:
    """Every integration DevPilot has, each backed by its fixture: the registry then holds every GPT function."""
    settings = make_settings(tmp_path, log_level="ERROR", **N8N)
    drive, _source = rag.fixture_runtime(drive_fixtures.FixtureDrive.research(), settings)
    n8n = build_n8n_runtime(settings, EGRESS, client=FakeN8n.with_active("devpilot-echo", "devpilot-notify"))
    selector = build_selector(settings, EGRESS, [_simulated("simulated", [])])
    return create_app(settings, llm=FakeLLM(), sandbox=ReplaySandbox.load(), coding=selector, repositories=FixtureSource(), drive=drive, n8n_runtime=n8n)


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "catalog.yaml"
    path.write_text(text, encoding="utf-8")
    return path


TOOL = """
tasks:
  code_generation: Write code
tools:
  aa:
    name: A
    role: r
    aliases: [alpha]
    description: d
    tasks: [code_generation]
    functions: []
    advantages: [x]
    limitations: [y]
    requirements: []
    evidence: []
"""


# --- The reviewed facts ----------------------------------------------------------------------------------------------


def test_the_shipped_catalog_loads_and_names_only_real_tools() -> None:
    facts = load_facts(SHIPPED)
    assert [tool.id for tool in facts.tools] == ["gpt", "opencode", "docker", "web_verifier", "n8n", "github", "google_drive"]
    assert [p.id for p in facts.planned] == ["opencode_via_n8n"]
    for tool in facts.tools:
        assert tool.advantages and tool.limitations and tool.evidence, tool.id


def test_evidence_is_never_stronger_than_what_ran() -> None:
    """Only GPT has a passed live run. OpenCode's one live build failed; n8n, GitHub and Drive never met the real service."""
    facts = load_facts(SHIPPED)
    assert {tool.id for tool in facts.tools if tool.verified_live} == {"gpt"}
    opencode = facts.get("opencode")
    assert opencode is not None
    assert [e.outcome for e in opencode.evidence if e.level == "live"] == ["failed"]
    assert opencode.strongest().level == "real_container"  # the strongest *passed* evidence, not the failed live run
    for tool_id in ("n8n", "github", "google_drive"):
        tool = facts.get(tool_id)
        assert tool is not None and {e.level for e in tool.evidence} == {"offline_test"}, tool_id


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (TOOL.replace("tasks: [code_generation]", "tasks: [flying]"), "unknown task 'flying'"),
        (TOOL + "  bb:\n" + TOOL.split("  aa:\n")[1], "names both aa and bb"),
        (TOOL + "planned:\n  aa:\n    name: A\n    description: d\n", "both built and planned"),
        (TOOL.replace("    evidence: []", "    evidence: []\n    benchmark: 99%"), "Extra inputs"),
        (TOOL.replace("    evidence: []", "    evidence:\n      - {level: rumour, outcome: passed, date: 2026-01-01, summary: s, source: s}"), "level"),
        (TOOL.replace("    advantages: [x]", "    advantages: []"), "advantages"),
        ("tools: []\n", "must define"),
    ],
)
def test_a_malformed_catalog_stops_the_app(tmp_path: Path, body: str, message: str) -> None:
    with pytest.raises(CatalogError, match=message):
        load_facts(write(tmp_path, body))
    with pytest.raises(CatalogError):
        create_app(make_settings(tmp_path, log_level="ERROR", tool_catalog_file=write(tmp_path, body)), llm=FakeLLM())


def test_every_gpt_function_belongs_to_a_catalog_tool_and_every_listed_function_exists(tmp_path: Path) -> None:
    """With every integration wired, the registry and the catalog name exactly the same functions."""
    app = everything_app(tmp_path)
    facts = load_facts(SHIPPED)
    listed = {function for tool in facts.tools for function in tool.functions}
    registered = set(app.state.registry.names)
    assert registered - listed == set(), "registered functions no catalog tool claims"
    assert listed - registered == set(), "catalog functions that do not exist"


# --- Readiness from the running backend ------------------------------------------------------------------------------


def test_a_bare_backend_reports_what_each_tool_still_needs(client: TestClient, auth: dict[str, str]) -> None:
    tools = {tool["id"]: tool for tool in client.get("/api/v1/tools", headers=auth).json()["tools"]}
    assert tools["opencode"]["readiness"]["status"] == "not_configured" and "OPENCODE_ENABLED" in tools["opencode"]["readiness"]["detail"]
    assert tools["docker"]["readiness"] == {"status": "not_configured", "detail": "no sandbox is configured in this backend, so nothing can run tests"}
    assert tools["n8n"]["readiness"]["detail"] == "N8N_ENABLED is not true in this backend"
    assert tools["github"]["readiness"]["detail"] == "REPOSITORY_SOURCE is off in this backend"
    assert tools["google_drive"]["readiness"]["detail"] == "DRIVE_SOURCE is off in this backend"
    assert tools["n8n"]["functions"] == [] and "compare_tools" in tools["gpt"]["functions"]


def test_fixture_backed_tools_are_simulated_never_ready(tmp_path: Path) -> None:
    with TestClient(everything_app(tmp_path)) as client:
        tools = {tool["id"]: tool for tool in client.get("/api/v1/tools", headers=HEADERS).json()["tools"]}
    for tool_id in ("gpt", "opencode", "docker", "n8n", "github", "google_drive"):
        assert tools[tool_id]["readiness"]["status"] == "simulated", (tool_id, tools[tool_id]["readiness"])
    assert tools["n8n"]["functions"] == ["trigger_workflow", "get_workflow_result"]


def test_a_real_drive_without_encryption_is_blocked_and_says_why(tmp_path: Path) -> None:
    from app.tools.adapters.drive import build_drive_runtime

    settings = make_settings(tmp_path, log_level="ERROR", drive_source="google", google_oauth_client_id="client.apps.googleusercontent.com")
    app = create_app(settings, llm=FakeLLM(), drive=build_drive_runtime(settings, EGRESS))
    with TestClient(app) as client:
        drive = next(tool for tool in client.get("/api/v1/tools", headers=HEADERS).json()["tools"] if tool["id"] == "google_drive")
    assert drive["readiness"]["status"] == "blocked"
    assert "encrypted" in drive["readiness"]["detail"] or "OAuth provider" in drive["readiness"]["detail"]


# --- Comparisons -----------------------------------------------------------------------------------------------------


def test_compare_n8n_and_cloudflare_reports_cloudflare_as_unknown_with_nothing_invented(client: TestClient, auth: dict[str, str]) -> None:
    answer = client.post("/api/v1/tools/compare", headers=auth, json={"question": "Compare n8n and Cloudflare"}).json()
    assert answer["kind"] == "comparison"
    assert [tool["id"] for tool in answer["tools"]] == ["n8n"]
    assert answer["unknown"] == [{"name": "Cloudflare", "note": answer["unknown"][0]["note"]}]
    assert "no tested capabilities" in answer["unknown"][0]["note"]
    assert any("offline tests only" in note for note in answer["notes"])


@pytest.mark.parametrize(
    ("question", "tools", "unknown", "planned"),
    [
        ("OpenCode vs n8n for fixing bugs", ["opencode", "n8n"], [], []),
        ("compare opencode via n8n with zapier", [], ["zapier"], ["OpenCode through n8n"]),
        ("What is the difference between GitHub and Google Drive?", ["github", "google_drive"], [], []),
        ("Is Docker better than Vagrant?", ["docker"], ["Vagrant"], []),
        ("compare GPT, OpenCode and Cursor", ["gpt", "opencode"], ["Cursor"], []),
    ],
)
def test_questions_name_the_tools_they_mention(client: TestClient, auth: dict[str, str], question: str, tools: list[str], unknown: list[str], planned: list[str]) -> None:
    answer = client.post("/api/v1/tools/compare", headers=auth, json={"question": question}).json()
    assert [tool["id"] for tool in answer["tools"]] == tools
    assert [item["name"] for item in answer["unknown"]] == unknown
    assert [item["name"] for item in answer["planned"]] == planned


def test_a_planned_tool_is_never_compared_as_if_it_worked() -> None:
    catalog = ToolCatalog(load_facts(SHIPPED))
    answer = catalog.compare(["opencode_via_n8n"])
    assert answer["tools"] == [] and "planned, not built" in answer["planned"][0]["note"]
    assert all(entry["readiness"]["status"] == "planned" for entry in catalog.describe()["planned"])


def test_which_tool_is_best_recommends_from_the_tasks_it_reads(client: TestClient, auth: dict[str, str]) -> None:
    answer = client.post("/api/v1/tools/compare", headers=auth, json={"question": "Which tool is best for my project? A website with a contact form"}).json()
    assert answer["kind"] == "recommendation"
    picks = {pick["task"]: pick["tool"] for pick in answer["recommendation"]["picks"]}
    assert picks["code_generation"] == "opencode" and picks["browser_verification"] == "web_verifier"
    assert all(pick["readiness"] != "ready" for pick in answer["recommendation"]["picks"])  # nothing is configured in this backend


def test_recommendation_prefers_a_usable_tool_then_stronger_evidence() -> None:
    facts = load_facts(SHIPPED)
    catalog = ToolCatalog(facts)
    statuses = {"opencode": "not_configured", "docker": "ready"}
    import app.toolcatalog.catalog as module

    original = module.readiness
    module.readiness = lambda tool_id, _state: Readiness(statuses.get(tool_id, "blocked"), "test")  # type: ignore[assignment]
    try:
        catalog.attach(SimpleNamespace(registry=None))
        picks = {p["task"]: p for p in catalog.recommend(["test_execution", "code_generation", "rag_indexing", "nonsense"])["picks"]}
    finally:
        module.readiness = original
    assert picks["test_execution"]["tool"] == "docker" and picks["test_execution"]["readiness"] == "ready"
    assert picks["code_generation"]["tool"] == "opencode" and "not configured" in picks["code_generation"]["why"]
    assert "nonsense" not in picks


def test_the_routes_need_the_token_and_something_to_compare(client: TestClient, auth: dict[str, str]) -> None:
    assert client.get("/api/v1/tools").status_code == 401
    assert client.post("/api/v1/tools/compare", json={"names": ["n8n"]}).status_code == 401
    assert client.post("/api/v1/tools/compare", headers=auth, json={}).status_code == 422
    assert client.post("/api/v1/tools/compare", headers=auth, json={"names": ["n8n"], "extra": 1}).status_code == 422
    by_name = client.post("/api/v1/tools/compare", headers=auth, json={"names": ["opencode", "docker"], "tasks": ["test_execution"]}).json()
    assert by_name["shared_tasks"] == [] and by_name["recommendation"]["picks"][0]["tool"] == "docker"


# --- GPT's function --------------------------------------------------------------------------------------------------


def test_compare_tools_is_a_strict_read_only_function_in_every_working_state(app: Any) -> None:
    schema = strict_json_schema(CompareToolsArgs)
    assert schema["additionalProperties"] is False and schema["required"] == ["names", "tasks"]
    spec = app.state.registry.get("compare_tools")
    assert spec.risk.value == "READ" and spec.states is None
    for state in (RunState.ANALYZING, RunState.PLANNING, RunState.IMPLEMENTING, RunState.DOCUMENTING):
        assert "compare_tools" in [s["name"] for s in app.state.registry.schemas_for(state)]


def test_compare_tools_answers_gpt_briefly_from_the_catalog(app: Any) -> None:
    from app.tools.adapters.catalog import compare_tools

    catalog = app.state.tool_catalog
    listing = compare_tools(catalog, None, CompareToolsArgs(names=[], tasks=["workflow_automation"]))  # type: ignore[arg-type]
    assert {tool["id"] for tool in listing["tools"]} >= {"gpt", "opencode", "n8n"} and "evidence" not in listing["tools"][0]
    assert listing["recommendation"]["picks"][0]["tool"] == "n8n"
    compared = compare_tools(catalog, None, CompareToolsArgs(names=["n8n", "Cloudflare"], tasks=None))  # type: ignore[arg-type]
    assert [t["id"] for t in compared["tools"]] == ["n8n"] and compared["unknown"][0]["name"] == "Cloudflare"
