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

    def run(self, cancel: threading.Event | None = None) -> BackendResult:
        try:
            result = self.verifier.verify(self.files, self.kind, component=self.component, cancel=cancel)
        except (WebVerificationError, OSError) as exc:
            result = WebResult("NOT_RUN", str(exc), self.kind, exec_status="UNAVAILABLE")
        status = ExecStatus(result.exec_status)
        if not result.passed and status is ExecStatus.SUCCEEDED:
            status = ExecStatus.FAILED
        return BackendResult(ExecResult(
            status, "website-verification", "", exit_code=0 if result.passed else None,
            duration_ms=result.duration_ms, cleanup=result.cleanup, detail=result.reason,
        ), result)


def judge_website(result: WebResult, criteria: tuple[Criterion, ...], digest: str) -> VerificationResult:
    """Smoke evidence is useful for repair, but is never independent acceptance.

Until a frozen browser acceptance suite exists, even a healthy website remains
NOT_RUN at the delivery gate. Failed smoke checks still feed bounded recovery.
"""
    counts = TestCounts(readable=False)
    verification = VerificationResult(
        Verdict(result.verdict), result.reason, result.exec_status, 0 if result.passed else None,
        counts, judge_criteria(criteria, counts), result.output_tail, "", result.duration_ms,
        result.cleanup, digest,
    )
    if result.passed:
        return replace(verification, verdict=Verdict.NOT_RUN,
                       reason="website smoke checks passed; independent website acceptance has not run")
    return verification
