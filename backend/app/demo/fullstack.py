"""The offline full-stack rehearsal: `python -m app.demo.fullstack`. **Simulated throughout; nothing is built or run.**

One React + FastAPI request through the real API, orchestrator, gates, approval queue, repository tools, evaluator,
importer, plan gate and verification readiness. GPT is FakeLLM; the repositories are fixtures (never GitHub); no model,
Docker container, network or package is used. It shows, and checks, each case the readiness work must handle:
a suitable back-end repository reused; an unsuitable front-end repository, and a front end built from scratch instead;
a repository import a person denies, then approves; a two-component plan whose tasks depend on each other; and the
safe end when no verification runtime exists: the run stops by policy, says why for each component, and writes nothing.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import tempfile
import warnings
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.config import REPO_ROOT, Settings
from app.core.egress import EgressPolicy
from app.db.models import Approval, ApprovalStatus
from app.demo import weather
from app.demo.competition import _simulated
from app.orchestrator import approvals
from app.orchestrator.fake_llm import FakeLLM
from app.repos.fixtures import OWNER, FixtureSource
from app.sandbox.replay import ReplaySandbox
from app.selection.catalog import build_selector

API = "/api/v1"
OUT_DIR = REPO_ROOT / "data" / "fullstack-rehearsal"
BACKEND = f"{OWNER}/fastapi-weather-api"
DECISIONS = ["approve", "approve", "deny", "approve"]  # backend search, frontend search, first import, second import


def rehearse(data_dir: Path) -> dict[str, Any]:
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=".*httpx.*")
        from fastapi.testclient import TestClient

    from app.main import create_app

    settings = Settings(
        _env_file=None, devpilot_api_token=secrets.token_urlsafe(32), database_url=f"sqlite:///{(data_dir / 'rehearsal.db').as_posix()}", workspaces_dir=data_dir / "workspaces",
        coding_checkpoints_dir=data_dir / "checkpoints", coding_work_dir=data_dir / "work", log_level="ERROR",
    )  # type: ignore[call-arg]  # fmt: skip
    source = FixtureSource()
    commit = source.detail(BACKEND).commit
    selector = build_selector(settings, EgressPolicy.from_file(settings.egress_allowlist_file), [_simulated("simulated", [])])
    app = create_app(settings, llm=FakeLLM(*weather.fullstack_steps(BACKEND, commit)), sandbox=ReplaySandbox.load(), coding=selector, repositories=source)
    headers = {"Authorization": f"Bearer {settings.devpilot_api_token.get_secret_value()}"}
    client = TestClient(app)  # no lifespan: the run is advanced here, as the worker would
    try:
        project = client.post(f"{API}/projects", json={"name": "Weather application", "requirement_text": weather.REQUIREMENT, "acceptance": weather.ACCEPTANCE_SUITE.to_dict()}, headers=headers).json()
        run_id = client.post(f"{API}/projects/{project['id']}/runs", headers=headers).json()["id"]
        orchestrator, decided, fetched_at_decision = app.state.orchestrator, [], []
        orchestrator.advance(run_id)
        for decision in DECISIONS:
            with app.state.session_factory.begin() as s:
                approval = s.scalars(select(Approval).where(Approval.run_id == run_id, Approval.status == ApprovalStatus.PENDING)).one()
                decided.append({"tool": approval.tool, "risk": approval.risk, "decision": decision, "arguments": json.loads(approval.payload_json)})
                fetched_at_decision.append(list(source.calls))
                approvals.decide(s, approval, decision=decision, response=None, executor=app.state.executor, events=app.state.events, redactor=app.state.redactor)  # type: ignore[arg-type]
            orchestrator.advance(run_id)
        run = client.get(f"{API}/runs/{run_id}", headers=headers).json()
        report = client.get(f"{API}/runs/{run_id}/report", headers=headers).json()
        files = client.get(f"{API}/projects/{project['id']}/files", headers=headers).json()["files"]
    finally:
        client.close()
        app.state.engine.dispose()
    return {"run": run, "report": report, "approvals": decided, "fetched_before_each_decision": fetched_at_decision, "fetched": source.calls, "project_files": files}


def checks(result: dict[str, Any]) -> dict[str, bool]:
    report, decisions = result["report"], {d["component"]: d for d in result["report"]["repositories"].get("decisions", [])}
    blocked = report.get("verification_blocked") or {}
    denied_import = result["approvals"][2]
    return {
        "two components planned": [c["name"] for c in report["components"]] == ["backend", "frontend"],
        "every task has a component, and the dependencies were kept": [(t["component"], t["depends_on"]) for t in report["tasks"]] == [("backend", []), ("backend", [1]), ("frontend", [1]), ("frontend", [3])],
        "the back end reuses a suitable repository": decisions.get("backend", {}).get("action") == "reuse" and decisions["backend"]["full_name"] == BACKEND,
        "the front end builds from scratch: no suitable repository": decisions.get("frontend", {}).get("action") == "build",
        "front-end requirements were not skipped": any(t["component"] == "frontend" for t in report["tasks"]),
        "every search and import waited for a person": [d["risk"] for d in result["approvals"]] == ["EXTERNAL_NETWORK"] * 4,
        "the denied import fetched nothing": denied_import["decision"] == "deny" and ("archive", BACKEND) not in result["fetched_before_each_decision"][3],
        "the approved import went into backend/ with its licence": {"backend/LICENSE", "backend/THIRD_PARTY_NOTICES.md", "backend/app/main.py"} <= set(result["project_files"]),
        "nothing was imported for the front end": not any(f.startswith("frontend/") for f in result["project_files"]),
        "verification is blocked for both components, with reasons": sorted(b["component"] for b in blocked.get("components", [])) == ["backend", "frontend"],
        "the run stopped safely by policy": result["run"]["state"] == "STOPPED_BY_POLICY" and result["run"]["outcome"] == "verification_unavailable",
        "no code was generated and nothing was run": report["coding_attempts"] == [] and report["verifications"] == [],
        "the report names the fixtures as fixtures": "repository data (fixtures, not GitHub)" in report["provenance"]["simulated"],
    }


def render(result: dict[str, Any], passed: dict[str, bool]) -> str:
    lines = ["# Full-stack rehearsal: SIMULATED", "", "GPT is FakeLLM, the repositories are fixtures, and nothing was built, installed or run.", ""]
    lines += [f"- {'PASS' if ok else 'FAIL'}: {name}" for name, ok in passed.items()]
    lines += ["", f"Final state: {result['run']['state']} ({result['run']['outcome']}). {result['run']['status_detail']}", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="The offline full-stack rehearsal (no Docker, no model, no network)")
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="devpilot-fullstack-") as folder:
        result = rehearse(Path(folder))
    passed = checks(result)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "fullstack-rehearsal.json").write_text(json.dumps({**result, "checks": passed}, indent=2), encoding="utf-8")
    text = render(result, passed)
    (args.out / "fullstack-rehearsal.md").write_text(text, encoding="utf-8")
    print(text, flush=True)
    return 0 if all(passed.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
