"""The static website path through the real orchestrator, gates, coding adapter, verification, recovery and delivery.

GPT and the coding provider are scripted (app/demo/website.py) and the website verifier is a stand-in here, so none of this
is real generation or real browser execution: tests/integration/test_website_pipeline_docker.py runs the same scenario with
the real Docker verifier and Chromium.
"""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.db.models import Run, RunState
from app.demo import website
from app.orchestrator.fake_llm import call
from app.verification import artifact, browser_acceptance, recipes, web
from app.verification.backends import judge_website
from app.verification.results import Criterion
from app.verification.web import Stage, WebResult
from tests.unit.test_acceptance_flow import Seeing, gpt_saw
from tests.unit.test_coding_tools import Rig

IMAGE = "sha256:" + "f" * 64
VALIDATES = "Please enter a valid email"


class StandInVerifier:
    """Answers like the real verifier would for the scenario's site: AC-003 fails unless app.js validates the form.
    It records what it was given, so the tests can prove what reached the verifier and what did not."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def verify(self, files: Mapping[str, bytes], kind: str, *, component: str = "", cancel: Any = None, expect: Mapping[str, Any] | None = None, **_: Any) -> WebResult:
        self.calls.append({"files": dict(files), "kind": kind, "expect": expect})
        validates = VALIDATES.encode() in files.get("app.js", b"")
        results = tuple({"id": c["id"], "ok": c["id"] != "AC-003" or validates, "failed_step": None if (c["id"] != "AC-003" or validates) else 1,
                         "detail": "passed" if (c["id"] != "AC-003" or validates) else "that text is not visible", "errors": []} for c in (expect or {}).get("checks", []))  # fmt: skip
        stages = (Stage("serve", True, "ok"), Stage("http", True, "1 page"), Stage("assets", True, "2 assets"), Stage("browser", True, "rendered"),
                  Stage("acceptance", all(r["ok"] for r in results), "x"))  # fmt: skip
        ok = all(r["ok"] for r in results)
        return WebResult("PASSED" if ok else "FAILED", "every stage passed" if ok else "acceptance: AC-003 failed", kind, stages, ({"path": "/"},), acceptance=results,
                         exec_status="SUCCEEDED" if ok else "FAILED", cleanup="removed")  # fmt: skip


@pytest.fixture(autouse=True)
def website_ready(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A self-tested verifier image (stand-in ID) and the engine path enabled, as after `setup --web` on a ready install."""
    monkeypatch.setattr(recipes, "WEB_ENGINE_WIRED", True)
    web.approve_locally(IMAGE)
    yield
    web._APPROVED.clear()


def rig(tmp_path: Path, steps: list[Any] | None = None, verifier: Any = None) -> Rig:
    return Rig(tmp_path, *(steps or website.gpt_script()), launcher=Seeing([website.FIRST_ATTEMPT, website.REPAIR_ATTEMPT]), web=verifier or StandInVerifier())


def test_a_static_site_is_planned_coded_rejected_by_frozen_acceptance_repaired_and_delivered(tmp_path: Path) -> None:
    verifier = StandInVerifier()
    r = rig(tmp_path, verifier=verifier)
    assert r.advance().state is RunState.COMPLETED
    report = r.report()
    independent = report["acceptance"]
    run = r.h.run(r.run_id)
    assert independent["kind"] == "browser" and independent["verdict"] == "PASSED" and independent["suite_sha256"] == run.browser_acceptance_sha256
    assert [c["status"] for c in independent["criteria"]] == ["PASSED"] * 4
    first, second = (v["verdict"] for v in report["verifications"])
    assert (first, second) == ("FAILED", "PASSED") and report["run"]["repairs"] == 1
    assert [a["verdict"] for a in report["acceptance_runs"]] == ["FAILED", "PASSED"] and report["acceptance_runs"][0]["reason"] == "unmet MUST criteria: AC-003"
    assert {c["code"]: c["status"] for c in report["criteria"]} == {f"AC-00{i}": "PASSED" for i in range(1, 5)}
    assert sorted(r.h.workspace.list_files(r.project_id)) == ["README.md", "app.js", "index.html", "style.css"]
    assert VALIDATES in r.h.workspace.read_text(r.project_id, "app.js")  # the repaired file is what was delivered
    assert len(verifier.calls) == 2 and all(c["kind"] == "static-html" and len(c["expect"]["checks"]) == 4 for c in verifier.calls)


def test_the_checks_are_frozen_before_any_code_and_never_reach_the_coding_provider_or_the_workspace(tmp_path: Path) -> None:
    verifier = StandInVerifier()
    r = rig(tmp_path, verifier=verifier)
    r.advance()
    frozen = json.loads(r.h.run(r.run_id).browser_acceptance_json)
    assert [c["code"] for c in frozen] == ["AC-001", "AC-002", "AC-003", "AC-004"]
    markers = ("#project-list li", "expect_count", "expect_hidden", "ada@example.org", r.h.run(r.run_id).browser_acceptance_sha256)
    briefs = " ".join(r.launcher.briefs)
    assert r.launcher.briefs and not any(m in briefs for m in markers)  # the provider never sees a check
    assert not any(m.encode() in b"".join(call_["files"].values()) for call_ in verifier.calls for m in markers)  # nor is one in the candidate
    assert "expect.json" not in {p for call_ in verifier.calls for p in call_["files"]}
    events = r.h.events_of(r.run_id, "acceptance.frozen")
    assert len(events) == 1 and set(events[0]) == {"kind", "checks", "sha256"}  # the event records the count and hash only
    order = r.h.event_types(r.run_id)
    assert order.index("acceptance.frozen") < order.index("coding.started")


def test_gpt_hears_which_criterion_failed_and_why_but_never_the_checks(tmp_path: Path) -> None:
    r = rig(tmp_path)
    r.advance()
    failed = r.output("submit_implementation", 0)["data"]
    assert failed["verdict"] == "FAILED" and [u["code"] for u in failed["acceptance"]["unmet"]] == ["AC-003"]
    assert "then it first asks for valid input" in failed["acceptance"]["unmet"][0]["criterion"]
    assert "that text is not visible" in failed["acceptance"]["unmet"][0]["what_happened"]
    later = gpt_saw(r)
    assert "#project-list li" not in later and "expect_count" not in later and "ada@example.org" not in later


def test_checks_changed_after_freezing_stop_the_run_before_anything_is_judged(tmp_path: Path) -> None:
    verifier = StandInVerifier()
    holder: dict[str, Rig] = {}
    submit = call("submit_implementation", {"summary": "done"})

    def tamper_then_submit(request: Any, n: int) -> Any:  # someone edits the stored checks between coding and verification
        r_ = holder["rig"]
        with r_.h.sessions.begin() as s:
            run = s.get(Run, r_.run_id)
            run.browser_acceptance_json = run.browser_acceptance_json.replace("Send message", "Submit")
        return submit(request, n)

    steps = website.gpt_script()
    r = Rig(tmp_path, *steps[:4], tamper_then_submit, launcher=Seeing([website.REPAIR_ATTEMPT]), web=verifier)
    holder["rig"] = r
    final = r.advance()
    assert final.state is RunState.FAILED and final.outcome == "acceptance_suite_changed"
    assert verifier.calls == []  # nothing was judged by changed checks


def test_a_website_criterion_without_a_browser_check_is_rejected_and_browser_checks_are_validated(tmp_path: Path) -> None:
    missing = json.loads(json.dumps(website.CRITERIA))
    missing["criteria"][1]["browser"] = None
    bad_step = json.loads(json.dumps(website.CRITERIA))
    bad_step["criteria"][0]["browser"]["steps"] = [{"action": "click", "by": "role", "role": None, "target": "Menu", "value": None, "count": None}]
    r = Rig(tmp_path, call("submit_analysis", website.ANALYSIS), call("submit_plan", website.PLAN), call("submit_criteria", missing), call("submit_criteria", bad_step),
            call("submit_criteria", website.CRITERIA), launcher=Seeing([]), web=StandInVerifier())  # fmt: skip
    r.advance()
    first, second = r.output("submit_criteria", 0), r.output("submit_criteria", 1)
    assert first["ok"] is False and any("AC-002" in d and "needs a `browser` check" in d for d in first["error"]["details"])
    details = " ".join(second["error"]["details"])
    assert "needs `role`" in details and "at least one expect_* step" in details
    assert r.output("submit_criteria", 2)["data"]["accepted"] is True


def test_browser_checks_are_refused_for_a_python_project(tmp_path: Path) -> None:
    from app.demo import mvp

    criteria = json.loads(json.dumps(mvp._CALCULATOR_CRITERIA))
    criteria["criteria"][0]["browser"] = website.CRITERIA["criteria"][3]["browser"]
    r = Rig(tmp_path, call("submit_analysis", mvp._CALCULATOR_ANALYSIS), call("submit_plan", mvp._CALCULATOR_PLAN), call("submit_criteria", criteria), launcher=Seeing([]))
    r.advance()
    assert any("only for one-stack website projects" in d for d in r.output("submit_criteria", 0)["error"]["details"])


def test_the_delivery_gate_counts_browser_acceptance_as_independent_and_the_zip_holds_the_site_and_its_evidence(tmp_path: Path) -> None:
    r = rig(tmp_path)
    r.advance()
    report = r.report()
    assert artifact.unavailable_reason(report) is not None and "simulated" in artifact.unavailable_reason(report)  # scripted: correctly refused
    real_shaped = {**report, "provenance": {**report["provenance"], "simulated": [], "fully_real": True}}
    assert artifact.unavailable_reason(real_shaped) is None and artifact.verification_level(real_shaped) == artifact.INDEPENDENT
    files = {p: r.h.workspace.read_bytes(r.project_id, p) for p in r.h.workspace.list_files(r.project_id)}
    from app.core.redaction import Redactor

    redactor = Redactor([])
    selection, data = artifact.select(sorted(files), files.__getitem__, redactor)
    assert artifact.omitted_source_reason(selection) is None
    archive = zipfile.ZipFile(io.BytesIO(artifact.build_zip(real_shaped, selection, data, redactor, when=datetime.now(UTC))))
    names = archive.namelist()
    root = names[0].split("/")[0]
    assert {f"{root}/index.html", f"{root}/app.js", f"{root}/style.css"} <= set(names)
    evidence = [n for n in names if "devpilot-verification" in n]
    assert evidence, names
    text = " ".join(archive.read(n).decode("utf-8", "replace") for n in evidence)
    assert "browser checks frozen before any code was written" in text and "AC-003" in text


def test_without_frozen_checks_a_passing_site_is_still_never_delivered() -> None:
    result = WebResult("PASSED", "every stage passed", "static-html", (Stage("browser", True, "ok"),), ({"path": "/"},))
    verification, record = judge_website(result, (Criterion("AC-001", "MUST"),), "d")
    assert verification.verdict.value == "NOT_RUN" and record is None


def test_a_site_whose_own_checks_fail_does_not_pass_even_when_every_criterion_does() -> None:
    data, sha = browser_acceptance.freeze([{"code": "AC-001", "priority": "MUST", "start": "/", "steps": [{"action": "expect_title", "by": None, "role": None, "target": None, "value": "x", "count": None}]}])
    checks = browser_acceptance.thaw(data, sha)
    result = WebResult("FAILED", "browser: console error", "static-html", (Stage("browser", False, "console error"), Stage("acceptance", True, "1 passed")), ({"path": "/"},),
                       acceptance=({"id": "AC-001", "ok": True, "errors": []},))  # fmt: skip
    verification, record = judge_website(result, (Criterion("AC-001", "MUST"),), "d", checks)
    assert verification.verdict.value == "FAILED" and record is not None and "site's own checks failed" in record["reason"]


def test_a_check_that_did_not_run_is_never_passed() -> None:
    data, sha = browser_acceptance.freeze([{"code": c, "priority": "MUST", "start": "/", "steps": [{"action": "expect_title", "by": None, "role": None, "target": None, "value": "x", "count": None}]}
                                           for c in ("AC-001", "AC-002")])  # fmt: skip
    record = browser_acceptance.judge(browser_acceptance.thaw(data, sha), [{"id": "AC-001", "ok": True, "errors": []}], smoke_passed=True, smoke_reason="")
    assert record["verdict"] == "NOT_RUN" and [c["status"] for c in record["criteria"]] == ["PASSED", "NOT_RUN"]
