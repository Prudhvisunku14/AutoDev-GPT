"""Reading a test run and judging it against the acceptance criteria (ARCHITECTURE §9.3).

The verdict comes from what the sandbox really did: its exit status and the test runner's own output.
A run counts as passed only if the process exited 0, the runner's summary is readable and says OK, at
least one test ran, and every MUST criterion has a passing test that names it. So an empty suite, a
crash before the summary, or truncated output is never a pass.

A test proves a criterion by carrying its code in its name, such as `test_ac_002_divide_by_zero`. The
brief tells the coding engine to do that. A MUST criterion with no such test stays unverified, which
fails the run: nothing is assumed to be covered.

Limit: the tests come from the same engine as the code, so a pass shows the two agree, not that either
is right. Frozen, independently reviewed tests belong to the full Phase 5.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

from app.sandbox.spec import ExecResult, ExecStatus

_CASE = re.compile(r"^(?P<name>\w+) \((?P<where>[\w.]+)\)(?:\n.*?)?(?: \.\.\. | \.\.\.\n)?(?P<outcome>ok|FAIL|ERROR|skipped.*|expected failure|unexpected success)$", re.MULTILINE)
_PROBLEM = re.compile(r"^(?P<kind>FAIL|ERROR): (?P<name>[\w.]+) \((?P<where>[\w.]+)\)", re.MULTILINE)
_RAN = re.compile(r"^Ran (?P<count>\d+) tests? in ", re.MULTILINE)
_SUMMARY = re.compile(r"^(?P<verdict>OK|FAILED|NO TESTS RAN)(?: \((?P<details>[^)]*)\))?\s*$", re.MULTILINE)
_COUNT = re.compile(r"(failures|errors|skipped|expected failures|unexpected successes)=(\d+)")
_SYNTAX = re.compile(r"^(SyntaxError|IndentationError|TabError)\b", re.MULTILINE)
_IMPORT = re.compile(r"^(ImportError|ModuleNotFoundError)\b", re.MULTILINE)
_OUTPUT_TAIL_CHARS = 4_000


class Verdict(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"  # the tests ran and the project did not pass
    NOT_RUN = "NOT_RUN"  # the sandbox could not give an answer: says nothing about the code


class CriterionStatus(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    UNVERIFIED = "UNVERIFIED"  # no test names it


@dataclass(frozen=True)
class TestCounts:
    __test__ = False  # not a test class, despite the name

    readable: bool  # the runner's own summary was found
    ran: int = 0
    failures: int = 0
    errors: int = 0
    skipped: int = 0
    passed_names: tuple[str, ...] = ()
    failed_names: tuple[str, ...] = ()
    summary: str = ""

    @property
    def passed(self) -> int:
        return max(self.ran - self.failures - self.errors - self.skipped, 0)


@dataclass(frozen=True)
class CriterionResult:
    code: str
    priority: str
    status: CriterionStatus
    tests: tuple[str, ...] = ()


@dataclass(frozen=True)
class Criterion:
    code: str
    priority: str


@dataclass(frozen=True)
class VerificationResult:
    verdict: Verdict
    reason: str
    exec_status: str
    exit_code: int | None
    counts: TestCounts
    criteria: tuple[CriterionResult, ...]
    output_tail: str
    container: str
    duration_ms: int
    cleanup: str
    fingerprint: str = ""  # the tested files: what is delivered must be exactly this
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["verdict"] = self.verdict.value
        data["counts"]["passed"] = self.counts.passed
        data["criteria"] = [{**c, "status": str(c["status"])} for c in data["criteria"]]
        return data


def parse_unittest(output: str, *, truncated: bool = False) -> TestCounts:
    """`python -m unittest -v` writes to stderr. Truncated output is unreadable: the summary may be gone."""
    ran = _RAN.search(output)
    summaries = list(_SUMMARY.finditer(output))
    if truncated or ran is None or not summaries:
        return TestCounts(readable=False)
    last = summaries[-1]
    numbers = {name: int(value) for name, value in _COUNT.findall(last["details"] or "")}
    passed: list[str] = []
    failed: list[str] = []
    for match in _CASE.finditer(output):
        outcome = match["outcome"]
        if outcome == "ok" or outcome == "expected failure":
            passed.append(match["name"])
        elif outcome in ("FAIL", "ERROR", "unexpected success"):
            failed.append(match["name"])
    for match in _PROBLEM.finditer(output):
        failed.append(match["name"].rsplit(".", 1)[-1] if match["where"].startswith("unittest.loader") else match["name"])
    return TestCounts(
        readable=True,
        ran=int(ran["count"]),
        failures=numbers.get("failures", 0) + numbers.get("unexpected successes", 0),
        errors=numbers.get("errors", 0),
        skipped=numbers.get("skipped", 0),
        passed_names=tuple(dict.fromkeys(name for name in passed if name not in failed)),
        failed_names=tuple(dict.fromkeys(failed)),
        summary=last.group(0).strip(),
    )


def criterion_pattern(code: str) -> re.Pattern[str]:
    """AC-002 is proved by a test whose name holds `ac_002` or `ac002`, as a whole word: never `ac_0021`."""
    number = code.rsplit("-", 1)[-1]
    return re.compile(rf"(?<![0-9a-z])ac_?{re.escape(number)}(?![0-9])", re.IGNORECASE)


def judge_criteria(criteria: Iterable[Criterion], counts: TestCounts) -> tuple[CriterionResult, ...]:
    results: list[CriterionResult] = []
    for criterion in criteria:
        pattern = criterion_pattern(criterion.code)
        failed = tuple(t for t in counts.failed_names if pattern.search(t))
        passed = tuple(t for t in counts.passed_names if pattern.search(t))
        if failed:
            results.append(CriterionResult(criterion.code, criterion.priority, CriterionStatus.FAILED, failed + passed))
        elif passed:
            results.append(CriterionResult(criterion.code, criterion.priority, CriterionStatus.PASSED, passed))
        else:
            results.append(CriterionResult(criterion.code, criterion.priority, CriterionStatus.UNVERIFIED))
    return tuple(results)


def judge(result: ExecResult, criteria: Sequence[Criterion]) -> VerificationResult:
    """One sandbox run, judged. Every way of not knowing is NOT_RUN or FAILED, never PASSED."""
    output = result.stderr
    truncated = result.stderr_truncated
    tail = _tail(result)
    base = {"exec_status": result.status.value, "exit_code": result.exit_code, "output_tail": tail,
            "container": result.container, "duration_ms": result.duration_ms, "cleanup": result.cleanup}  # fmt: skip
    if result.status in (ExecStatus.UNAVAILABLE, ExecStatus.ERROR, ExecStatus.CANCELLED):
        reason = result.detail or f"the sandbox ended with {result.status.value}"
        return VerificationResult(Verdict.NOT_RUN, reason, counts=TestCounts(readable=False), criteria=(), **base)
    counts = parse_unittest(output, truncated=truncated)
    judged = judge_criteria(criteria, counts)
    reason = _failure_reason(result, counts, judged)
    verdict = Verdict.PASSED if reason is None else Verdict.FAILED
    return VerificationResult(verdict, reason or "every test and every MUST criterion passed", counts=counts, criteria=judged, **base)


def _failure_reason(result: ExecResult, counts: TestCounts, judged: Sequence[CriterionResult]) -> str | None:
    if result.status is not ExecStatus.SUCCEEDED and result.status is not ExecStatus.FAILED:
        return result.detail or f"the tests ended with {result.status.value}"
    if not counts.readable:
        return "the test runner's summary could not be read"
    if counts.ran == 0:
        return "no tests ran"
    if result.status is ExecStatus.FAILED or result.exit_code != 0 or not counts.summary.startswith("OK"):
        return f"{counts.failures} failed and {counts.errors} errored of {counts.ran} tests"
    must = [c for c in judged if c.priority == "MUST" and c.status is not CriterionStatus.PASSED]
    if must:
        return f"no passing test proves {', '.join(c.code for c in must)}"
    return None


def failure_kind(verification: VerificationResult) -> str:
    """Which recovery class this failure belongs to (app/recovery/engine.py)."""
    status = verification.exec_status
    if verification.verdict is Verdict.NOT_RUN:
        return "ENVIRONMENT"
    if status == ExecStatus.TIMEOUT.value:
        return "TEST_TIMEOUT"
    if status in (ExecStatus.MEMORY_LIMIT.value, ExecStatus.OUTPUT_LIMIT.value):
        return "RESOURCE_LIMIT"
    if _SYNTAX.search(verification.output_tail):
        return "SYNTAX_ERROR"
    if _IMPORT.search(verification.output_tail):
        return "IMPORT_ERROR"
    if verification.counts.readable and verification.counts.ran == 0:
        return "NO_TESTS"
    if verification.counts.readable and not verification.counts.failed_names and verification.exit_code == 0:
        return "MISSING_CRITERION_TEST"
    return "TEST_FAILURE"


def _tail(result: ExecResult) -> str:
    text = result.stderr or result.stdout
    return text[-_OUTPUT_TAIL_CHARS:]
