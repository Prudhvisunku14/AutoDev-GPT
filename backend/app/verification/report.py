"""The final execution report: what was asked, what ran, what passed, and what was real.

It is built from the run's own records, never from what a model or a provider said about itself. The
`provenance` block names what was simulated, so an offline run can never read as a real one.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.events import event_payload, list_events
from app.db.models import AcceptanceCriterion, PlanTask, Project, Requirement, Run

REAL_SANDBOX = "DockerSandbox"
REPLAY_SANDBOX = "ReplaySandbox"  # app/sandbox/replay.py: recorded results, offline
_EVENT_LIMIT = 5_000
_NOT_CONFIGURED = {"configured": False, "note": "No independent acceptance suite: the verdict rests on tests written by the coding provider."}


def build_report(session: Session, run: Run, project: Project, *, gpt: str, sandbox: str) -> dict[str, Any]:
    session.flush()
    events = list_events(session, run_id=run.id, limit=_EVENT_LIMIT)
    by_type: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        by_type.setdefault(event.type, []).append(event_payload(event))
    selections = by_type.get("selection.chosen", [])
    providers = sorted({s["provider"] for s in selections})
    tests = "test execution (replayed from recorded runs, not Docker)" if sandbox == REPLAY_SANDBOX else "Docker tests"
    searches = by_type.get("repository.searched", [])
    drive, indexed = by_type.get("drive.authorized", []), by_type.get("rag.indexed", [])
    simulated = [name for name, flag in (("GPT orchestration", gpt.startswith("FakeLLM")), ("coding provider", any(s["simulated"] for s in selections)),
                                         (tests, sandbox != REAL_SANDBOX), ("repository data (fixtures, not GitHub)", any(s["source"] == "fixture" for s in searches)),
                                         ("Google Drive data and consent (fixtures, not Google)", any(d.get("simulated") for d in drive)),
                                         ("embeddings (a hashing test double: lexical, not semantic)", any(i.get("simulated") for i in indexed))) if flag]  # fmt: skip
    delivered = by_type.get("delivery.finished", [{}])[-1].get("files", [])
    if run.acceptance_json is not None:
        independent = json.loads(run.acceptance_json)
    elif run.acceptance_suite_json is not None:
        independent = {"configured": True, "verdict": "NOT_RUN", "reason": "the code never reached the acceptance suite", "suite_sha256": run.acceptance_sha256, "criteria": []}
    else:
        independent = _NOT_CONFIGURED
    knowledge = None
    if drive or indexed:  # metadata only: the report never holds a document's text
        knowledge = {"authorizations": drive, "discovered": by_type.get("drive.discovered", []), "ingested": by_type.get("drive.ingested", []), "index": (indexed or [None])[-1],
                     "embedding_selection": (by_type.get("selection.embedding") or [None])[-1], "retrieval": (by_type.get("rag.retrieval_configured") or [None])[-1],
                     "blueprint": (by_type.get("rag.blueprint") or [None])[-1], "revoked": by_type.get("drive.revoked", [])}  # fmt: skip
    return {
        "project": {"id": project.id, "name": project.name, "requirement": project.requirement_text, "stack": project.stack},
        **({"knowledge": knowledge} if knowledge is not None else {}),
        "run": {"id": run.id, "state": run.state.value, "outcome": run.outcome, "detail": run.status_detail, "turns": run.turns,
                "tokens": run.tokens_in + run.tokens_out, "cost_usd": run.cost_usd if run.cost_known else None, "repairs": run.repairs},
        "requirements": [{"code": r.code, "text": r.text} for r in session.scalars(select(Requirement).where(Requirement.run_id == run.id).order_by(Requirement.code))],
        "tasks": [{"order": t.order, "title": t.title, "status": t.status, "component": t.component, "depends_on": json.loads(t.depends_on_json or "[]")}
                  for t in session.scalars(select(PlanTask).where(PlanTask.run_id == run.id).order_by(PlanTask.order))],
        "components": json.loads(run.plan_json or "{}").get("components") or [],
        "verification_blocked": (by_type.get("verification.blocked") or [None])[-1],
        "criteria": [{"code": c.code, "priority": c.priority, "status": c.status, "tests": json.loads(c.test_refs_json)}
                     for c in session.scalars(select(AcceptanceCriterion).where(AcceptanceCriterion.run_id == run.id).order_by(AcceptanceCriterion.code))],
        "selection": selections,
        "coding_attempts": [{k: a.get(k) for k in ("task_order", "provider", "status", "kept", "restored", "quarantined", "phase", "detail", "changes", "violations")}
                            for a in by_type.get("coding.finished", [])],
        "verifications": by_type.get("verification.finished", []),
        "website_verifications": by_type.get("verification.website", []),
        "repositories": {"searches": searches, "evaluated": by_type.get("repository.evaluated", []), "decision": (by_type.get("repository.decision") or [None])[-1],
                         "decisions": by_type.get("repository.decision", []), "imported": (by_type.get("repository.imported") or [None])[-1], "imports": by_type.get("repository.imported", [])},
        "acceptance": independent,
        "acceptance_runs": by_type.get("acceptance.finished", []),
        "recovery": by_type.get("recovery.decided", []),
        "recovery_outcomes": by_type.get("recovery.outcome", []),
        "delivered_files": delivered,
        "real_workspace_changed": bool(delivered),
        "provenance": {"gpt": gpt, "coding_providers": providers, "sandbox": sandbox, "simulated": simulated, "fully_real": not simulated},
    }  # fmt: skip


def render_markdown(report: dict[str, Any]) -> str:
    run = report["run"]
    lines = [f"# {report['project']['name']}: {run['state']}", "", f"Requirement: {report['project']['requirement']}", ""]
    provenance = report["provenance"]
    lines += [f"**Simulated in this run:** {', '.join(provenance['simulated'])}" if provenance["simulated"] else "**Nothing in this run was simulated.**", ""]
    knowledge = report.get("knowledge")
    if knowledge:
        index, retrieval = knowledge.get("index") or {}, knowledge.get("retrieval") or {}
        ingested = [d for call in knowledge["ingested"] for d in call["documents"]]
        lines += ["## Knowledge base", f"- Authorizations: {len(knowledge['authorizations'])}; revoked: {len(knowledge['revoked'])}",
                  f"- Documents: {sum(d['status'] == 'ingested' for d in ingested)} ingested of {len(ingested)} requested",
                  f"- Index: {index.get('chunks', 0)} chunks by {index.get('embedder', 'none')}{' (a test double)' if index.get('simulated') else ''}",
                  *[f"- Check: {c['question']} -> {'grounded, ' + str(len(c['citations'])) + ' citation(s)' if c['grounded'] else 'not in the documents'}" for c in retrieval.get("checks", [])], ""]  # fmt: skip
    lines += ["## Tasks", *[f"- {t['order']}. {t['title']}: {t['status']}" for t in report["tasks"]], ""]
    where = {REPLAY_SANDBOX: "replayed from recorded runs, not Docker", REAL_SANDBOX: "Docker sandbox"}.get(provenance.get("sandbox", ""), "no real sandbox")
    lines += [f"## The provider's tests ({where})"]
    for number, check in enumerate(report["verifications"], start=1):
        lines.append(f"- Run {number}: {check['verdict']}, {check['passed']} passed of {check['ran']}, exit code {check['exit_code']}, container {check['container']}. {check['reason']}")
        lines += [f"  - failed: {name}" for name in check["failed_names"]]
    if report.get("website_verifications"):
        lines += ["", "## Website smoke checks (not independent acceptance)"]
        for check in report["website_verifications"]:
            lines.append(f"- {check['kind']}: {check['verdict']}. {check['reason']} Cleanup: {check['cleanup']}.")
            lines += [f"  - {s['name']}: {'passed' if s['ok'] else 'failed'}; {s['detail']}" for s in check["stages"]]
    by_browser = (report.get("acceptance") or {}).get("kind") == "browser"
    lines += ["", "## Acceptance criteria (" + ("proved by the frozen browser checks below" if by_browser else "proved by the provider's own tests") + ")",
              *[f"- {c['code']} [{c['priority']}]: {c['status']}" for c in report["criteria"]], ""]  # fmt: skip
    independent = report.get("acceptance", _NOT_CONFIGURED)
    browser = independent.get("kind") == "browser"
    lines += ["## Independent acceptance (" + ("browser checks frozen before any code was written" if browser else "the evaluator's frozen suite") + ")"]
    if not independent.get("configured"):
        lines += [independent["note"], ""]
    else:
        runs = report.get("acceptance_runs", [])
        lines += [f"Acceptance run {n}: {a['verdict']}, {a['passed']} of {a['total']} criteria. {a['reason']}" for n, a in enumerate(runs, start=1)]
        lines += [f"Final: {independent['verdict']}: {independent['reason']}. Suite SHA-256 {independent.get('suite_sha256') or 'unknown'}."]
        lines += [f"- {c['code']} [{c['priority']}]: {c['status']}. {c['description']}" for c in independent.get("criteria", [])]
        lines += ["", "_The checks were written from the acceptance criteria before any code existed, frozen with the SHA-256 above, never shown to the coding provider, "
                  "and run as data by DevPilot's own browser runner in a no-network container. The site's scripts run in the same browser page they judge, so the "
                  "checks catch honest mistakes and self-graded passes; they are not an adversarial security boundary._" if browser else
                  "_The suite runs in the same Python process as the code it judges. It catches honest mistakes and self-graded passes, and it is not an adversarial security boundary._", ""]  # fmt: skip
    if report["recovery"]:
        lines += ["## Recovery", *[f"- {r.get('category') or r['failure_class']}: {r['action']}. {r['reason']}" + (f" Evidence: {r['evidence'][0]}" if r.get("evidence") else "") for r in report["recovery"]], ""]
    lines += ["## Delivered", *([f"- {f['path']} ({f['change']}, {f['bytes']} bytes)" for f in report["delivered_files"]] or ["Nothing was delivered. The project was not changed."]), ""]
    if run["detail"]:
        lines += [f"Outcome: {run['outcome']}. {run['detail']}", ""]
    return "\n".join(lines)
