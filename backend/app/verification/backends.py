"""Execution backends for fixed sandbox jobs and multi-stage website verification.

Backends execute policy-owned plans, never model-supplied commands. Website smoke
checks deliberately do not manufacture test counts or prove acceptance criteria.
"""
from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Protocol

from app.sandbox.docker_cli import SandboxUnavailable
from app.sandbox.runner import SandboxRunner
from app.sandbox.spec import ExecResult, ExecSpec, ExecStatus
from app.verification import browser_acceptance
from app.verification.browser_acceptance import FrozenChecks
from app.verification.results import Criterion, TestCounts, Verdict, VerificationResult, judge_criteria
from app.verification.web import WebResult, WebVerifier, WebVerificationError


@dataclass(frozen=True)
class BackendResult:
    execution: ExecResult
    website: WebResult | None = None


class VerificationBackend(Protocol):
    def run(self, cancel: threading.Event | None = None) -> BackendResult: ...


@dataclass(frozen=True)
class SingleStageBackend:
    sandbox: SandboxRunner
    spec: ExecSpec

    def run(self, cancel: threading.Event | None = None) -> BackendResult:
        try:
            result = self.sandbox.run(self.spec, cancel=cancel)
        except SandboxUnavailable as exc:
            result = ExecResult.unavailable(self.spec, str(exc))
        return BackendResult(result)


@dataclass(frozen=True)
class WebsiteBackend:
    verifier: WebVerifier
    files: Mapping[str, bytes]
    kind: str
    component: str = ""
    checks: FrozenChecks | None = None  # the run's frozen browser acceptance checks; mounted read-only into the verifier only

    def run(self, cancel: threading.Event | None = None) -> BackendResult:
        try:
            result = self.verifier.verify(self.files, self.kind, component=self.component, cancel=cancel, expect=self.checks.expect() if self.checks else None)
        except (WebVerificationError, OSError) as exc:
            result = WebResult("NOT_RUN", str(exc), self.kind, exec_status="UNAVAILABLE")
        status = ExecStatus(result.exec_status)
        if not result.passed and status is ExecStatus.SUCCEEDED:
            status = ExecStatus.FAILED
        return BackendResult(ExecResult(
            status, "website-verification", "", exit_code=0 if result.passed else None,
            duration_ms=result.duration_ms, cleanup=result.cleanup, detail=result.reason,
        ), result)


def check_name(code: str) -> str:
    """The name a frozen browser check reports under: `browser_ac_002` proves AC-002 by the shared criterion pattern."""
    return "browser_" + code.lower().replace("-", "_")


def judge_website(result: WebResult, criteria: tuple[Criterion, ...], digest: str, checks: FrozenChecks | None = None) -> tuple[VerificationResult, dict[str, object] | None]:
    """The website run, judged. Returns the verification and, when browser checks were frozen, the acceptance record.

    Without frozen checks, smoke evidence is never independent acceptance: a healthy site stays NOT_RUN at the delivery gate,
    and failed smoke checks still feed bounded recovery. With them, the verdict is PASSED only when the built-in checks passed
    and every MUST criterion's own check passed in the browser.
    """
    if checks is None:
        counts = TestCounts(readable=False)
        verification = VerificationResult(
            Verdict(result.verdict), result.reason, result.exec_status, 0 if result.passed else None,
            counts, judge_criteria(criteria, counts), result.describe(), "", result.duration_ms,
            result.cleanup, digest,
        )
        if result.passed:
            return replace(verification, verdict=Verdict.NOT_RUN, reason="website smoke checks passed; independent website acceptance has not run"), None
        return verification, None
    if result.verdict == "NOT_RUN" and not result.acceptance:  # the verifier gave no answer: nothing was judged
        record = browser_acceptance.judge(checks, None, smoke_passed=False, smoke_reason=result.reason)
        counts = TestCounts(readable=False)
        return VerificationResult(Verdict.NOT_RUN, result.reason, result.exec_status, None, counts, judge_criteria(criteria, counts), result.describe(), "",
                                  result.duration_ms, result.cleanup, digest), record  # fmt: skip
    record = browser_acceptance.judge(checks, result.acceptance, smoke_passed=result.smoke_passed, smoke_reason=result.reason)
    outcomes = record["criteria"]
    passed_names = tuple(check_name(c["code"]) for c in outcomes if c["status"] == browser_acceptance.PASSED)
    failed_names = tuple(check_name(c["code"]) for c in outcomes if c["status"] == browser_acceptance.FAILED)
    counts = TestCounts(True, int(record["ran"]), len(failed_names), 0, 0, passed_names, failed_names, f"{record['passed']} of {len(outcomes)} browser acceptance checks passed")
    judged = judge_criteria(criteria, counts)
    lines = [result.describe()] + [f"  {c['code']} [{c['priority']}] {c['status']}: {c['detail']}" for c in outcomes if c["status"] != browser_acceptance.PASSED]
    verdict = {browser_acceptance.PASSED: Verdict.PASSED, browser_acceptance.FAILED: Verdict.FAILED}.get(str(record["verdict"]), Verdict.NOT_RUN)
    exit_code = 0 if verdict is Verdict.PASSED else 1
    return VerificationResult(verdict, str(record["reason"]), "SUCCEEDED" if verdict is Verdict.PASSED else ("FAILED" if verdict is Verdict.FAILED else result.exec_status),
                              exit_code, counts, judged, "\n".join(lines)[-4_000:], "", result.duration_ms, result.cleanup, digest), record  # fmt: skip
