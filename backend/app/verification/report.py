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

_REAL_SANDBOX = "DockerSandbox"
_EVENT_LIMIT = 5_000


def build_report(session: Session, run: Run, project: Project, *, gpt: str, sandbox: str) -> dict[str, Any]:
    session.flush()
    events = list_events(session, run_id=run.id, limit=_EVENT_LIMIT)
    by_type: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        by_type.setdefault(event.type, []).append(event_payload(event))
    selections = by_type.get("selection.chosen", [])
    providers = sorted({s["provider"] for s in selections})
    simulated = [name for name, flag in (("GPT orchestration", gpt.startswith("FakeLLM")), ("coding provider", any(s["simulated"] for s in selections)),
                                         ("Docker tests", sandbox != _REAL_SANDBOX)) if flag]  # fmt: skip
    delivered = by_type.get("delivery.finished", [{}])[-1].get("files", [])
    return {
        "project": {"id": project.id, "name": project.name, "requirement": project.requirement_text, "stack": project.stack},
        "run": {"id": run.id, "state": run.state.value, "outcome": run.outcome, "detail": run.status_detail, "turns": run.turns,
                "tokens": run.tokens_in + run.tokens_out, "cost_usd": run.cost_usd if run.cost_known else None, "repairs": run.repairs},
        "requirements": [{"code": r.code, "text": r.text} for r in session.scalars(select(Requirement).where(Requirement.run_id == run.id).order_by(Requirement.code))],
        "tasks": [{"order": t.order, "title": t.title, "status": t.status} for t in session.scalars(select(PlanTask).where(PlanTask.run_id == run.id).order_by(PlanTask.order))],
        "criteria": [{"code": c.code, "priority": c.priority, "status": c.status, "tests": json.loads(c.test_refs_json)}
                     for c in session.scalars(select(AcceptanceCriterion).where(AcceptanceCriterion.run_id == run.id).order_by(AcceptanceCriterion.code))],
        "selection": selections,
        "coding_attempts": [{k: a.get(k) for k in ("task_order", "provider", "status", "kept", "restored", "quarantined", "phase", "detail", "changes", "violations")}
                            for a in by_type.get("coding.finished", [])],
        "verifications": by_type.get("verification.finished", []),
        "recovery": by_type.get("recovery.decided", []),
        "delivered_files": delivered,
        "real_workspace_changed": bool(delivered),
        "provenance": {"gpt": gpt, "coding_providers": providers, "sandbox": sandbox, "simulated": simulated, "fully_real": not simulated},
    }  # fmt: skip


def render_markdown(report: dict[str, Any]) -> str:
    run = report["run"]
    lines = [f"# {report['project']['name']}: {run['state']}", "", f"Requirement: {report['project']['requirement']}", ""]
    provenance = report["provenance"]
    lines += [f"**Simulated in this run:** {', '.join(provenance['simulated'])}" if provenance["simulated"] else "**Nothing in this run was simulated.**", ""]
    lines += ["## Tasks", *[f"- {t['order']}. {t['title']}: {t['status']}" for t in report["tasks"]], ""]
    lines += ["## Tests in the sandbox"]
    for number, check in enumerate(report["verifications"], start=1):
        lines.append(f"- Run {number}: {check['verdict']}, {check['passed']} passed of {check['ran']}, exit code {check['exit_code']}, container {check['container']}. {check['reason']}")
        lines += [f"  - failed: {name}" for name in check["failed_names"]]
    lines += ["", "## Acceptance criteria", *[f"- {c['code']} [{c['priority']}]: {c['status']}" for c in report["criteria"]], ""]
    if report["recovery"]:
        lines += ["## Recovery", *[f"- {r['failure_class']}: {r['action']}. {r['reason']}" for r in report["recovery"]], ""]
    lines += ["## Delivered", *([f"- {f['path']} ({f['change']}, {f['bytes']} bytes)" for f in report["delivered_files"]] or ["Nothing was delivered. The project was not changed."]), ""]
    if run["detail"]:
        lines += [f"Outcome: {run['outcome']}. {run['detail']}", ""]
    return "\n".join(lines)
