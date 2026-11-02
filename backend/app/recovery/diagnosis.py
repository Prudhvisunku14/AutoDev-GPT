"""What went wrong, in a form a recovery decision can use (ARCHITECTURE §9.2).

A failed attempt or test run becomes a `Diagnosis`: its category, the evidence, how sure that is, whether
recovery is safe, and which actions could apply, best first. Structured signals are read first, because
what a program prints cannot fake them: the attempt's lifecycle phase, the audit, the sandbox's exit status
and the test runner's own counts. Text patterns only tell one fault in the code from another, so nothing a
program prints can make an unsafe state look safe.

Two rules fail closed:

- A security or permission failure is never recoverable.
- An attempt not proved undone is ambiguous. That means any phase outside `_PROVEN_UNDONE`, a test
  container nobody could remove, or an answer DevPilot cannot interpret. A status or phase added later
  lands here until someone decides otherwise.

An attempt whose prompt delivery was ambiguous, but whose writer is confirmed gone and whose workspace was
restored and compared (A1, A2), is no longer ambiguous: that proof is what RESTORED means.

The fingerprint tells whether two failures are the same one: paths, addresses and numbers are removed, so
`-3 != 3` and `-4 != 4` match. Test names are kept. This module decides nothing and has no effects.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any

from app.coding.types import AttemptPhase, CodingStatus
from app.recovery.engine import Action
from app.sandbox.spec import ExecStatus
from app.verification.results import Verdict, VerificationResult, failure_kind

CODING, VERIFICATION = "coding", "verification"
_EVIDENCE_LINES, _EVIDENCE_CHARS = 5, 200


class FailureCategory(StrEnum):
    SYNTAX_ERROR = "SYNTAX_ERROR"  # the code does not parse
    TEST_FAILURE = "TEST_FAILURE"  # the tests ran, and an assertion failed
    MISSING_DEPENDENCY = "MISSING_DEPENDENCY"  # a module could not be imported
    RUNTIME_EXCEPTION = "RUNTIME_EXCEPTION"  # the code raised while a test ran
    TIMEOUT = "TIMEOUT"  # the tests, or the coding attempt, ran out of time
    RESOURCE_EXHAUSTION = "RESOURCE_EXHAUSTION"  # memory, output or the task's tokens ran out
    TOOL_UNAVAILABLE = "TOOL_UNAVAILABLE"  # the provider or the sandbox could not do the work
    INVALID_CODE = "INVALID_CODE"  # the result is not usable: no tests, an unproved criterion, an oversized file
    SECURITY_VIOLATION = "SECURITY_VIOLATION"  # a policy breach, refused credentials or an unsafe configuration
    AMBIGUOUS_STATE = "AMBIGUOUS_STATE"  # what happened cannot be proved
    UNKNOWN = "UNKNOWN"  # no rule matched


class Confidence(StrEnum):
    HIGH = "HIGH"  # from a structured signal
    MEDIUM = "MEDIUM"  # from text in a readable test run, or a broad status
    LOW = "LOW"  # no rule matched


UNSAFE = frozenset({FailureCategory.SECURITY_VIOLATION, FailureCategory.AMBIGUOUS_STATE})

# What could apply, best first. The policy takes the first one that is allowed now.
_FIX_THE_CODE = (Action.REPAIR, Action.SWITCH_TOOL, Action.STOP)
_TEST_AGAIN = (Action.RETRY, Action.STOP)
_TRY_ELSEWHERE = (Action.SWITCH_TOOL, Action.RETRY, Action.STOP)
_NEEDS_TIME = (Action.RECONFIGURE, Action.SWITCH_TOOL, Action.RETRY, Action.STOP)
_ONLY_ANOTHER_TOOL = (Action.SWITCH_TOOL, Action.STOP)
_ONLY_STOP = (Action.STOP,)

# Nothing is left of an attempt in these: it never started, it was refused, or it was restored and compared.
_PROVEN_UNDONE = frozenset({AttemptPhase.ABANDONED.value, AttemptPhase.REJECTED.value, AttemptPhase.RESTORED.value})
_REFUSED_ACCESS = frozenset({CodingStatus.POLICY_VIOLATION, CodingStatus.AUTH, CodingStatus.INCOMPATIBLE})
# The only audit findings that describe the output and not a breach. Anything else stays a violation.
_SHAPE_VIOLATION = re.compile(r"\d+ bytes is over the \d+-byte limit$|holds \d+ files, over the \d+-file limit$")

_EXCEPTION = re.compile(r"^(?P<type>\w*(?:Error|Exception)): ?(?P<message>.*)$", re.MULTILINE)
_FRAME = re.compile(r'File "(?P<file>[^"]+)", line (?P<line>\d+)')
_MODULE = re.compile(r"No module named '(?P<module>[\w.]+)'")
_PATH = re.compile(r"[\w.-]*[/\\][\w./\\-]*")
_VOLATILE = re.compile(r"0x[0-9a-fA-F]+|\d+")


@dataclass(frozen=True)
class AttemptSignal:
    """What is known about a coding attempt that was not kept."""

    task: int
    status: CodingStatus
    phase: str  # the attempt's final lifecycle phase
    detail: str = ""
    violations: tuple[str, ...] = ()


@dataclass(frozen=True)
class Diagnosis:
    category: FailureCategory
    failure_class: str  # the finer class the bounded policy works from
    source: str  # CODING or VERIFICATION
    confidence: Confidence
    evidence: tuple[str, ...]
    actions: tuple[Action, ...]
    fingerprint: str
    location: str | None = None  # file:line, when the output names one

    @property
    def recovery_safe(self) -> bool:
        return self.category not in UNSAFE

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value, "failure_class": self.failure_class, "source": self.source, "confidence": self.confidence.value, "evidence": list(self.evidence),
            "recovery_safe": self.recovery_safe, "actions": [a.value for a in self.actions], "fingerprint": self.fingerprint, "location": self.location,
        }  # fmt: skip


def _line(text: str) -> str:
    return " ".join(text.split())[:_EVIDENCE_CHARS]


def _lines(*items: str) -> tuple[str, ...]:
    return tuple(line for line in (_line(item) for item in items) if line)[:_EVIDENCE_LINES]


def fingerprint(category: FailureCategory, *exact: str, varying: str = "") -> str:
    """`exact` parts are compared as they are, and `varying` text without its paths and numbers."""
    normal = _VOLATILE.sub("#", _PATH.sub("<path>", varying)).lower()
    return hashlib.sha256("\0".join((category.value, *exact, normal)).encode("utf-8")).hexdigest()[:16]


def _exceptions(output: str) -> list[tuple[str, str]]:
    return [(m["type"], m["message"].strip()) for m in _EXCEPTION.finditer(output)]


def _location(output: str) -> str | None:
    """The last frame in the project's own files, where a traceback points at the fault."""
    frames = [(m["file"].replace("\\", "/"), m["line"]) for m in _FRAME.finditer(output)]
    own = [(path, line) for path, line in frames if "/lib/python" not in path]
    return f"{PurePosixPath(own[-1][0]).name}:{own[-1][1]}" if own else None


def classify_verification(v: VerificationResult) -> Diagnosis:
    """A test run that did not pass. Structured facts first, then the output."""
    kind = failure_kind(v)
    tail = v.output_tail

    def found(category: FailureCategory, confidence: Confidence, actions: tuple[Action, ...], *evidence: str, varying: str = "", exact: tuple[str, ...] = ()) -> Diagnosis:
        return Diagnosis(category, kind, VERIFICATION, confidence, _lines(*evidence), actions, fingerprint(category, *exact, varying=varying), _location(tail))

    if v.cleanup == "failed":  # a test container may still be there, so the state is not known
        return found(FailureCategory.AMBIGUOUS_STATE, Confidence.HIGH, _ONLY_STOP, f"the test container {v.container} could not be removed", v.reason, varying=v.reason)
    if v.verdict is Verdict.NOT_RUN:  # known by its status alone: docker's wording can change between tries, and only the streak bounds a rerun
        return found(FailureCategory.TOOL_UNAVAILABLE, Confidence.HIGH, _TEST_AGAIN, f"the sandbox ended with {v.exec_status}", v.reason, exact=(v.exec_status,))
    if v.exec_status == ExecStatus.TIMEOUT.value:
        return found(FailureCategory.TIMEOUT, Confidence.HIGH, _FIX_THE_CODE, v.reason, varying=v.reason)
    if v.exec_status in (ExecStatus.MEMORY_LIMIT.value, ExecStatus.OUTPUT_LIMIT.value):
        return found(FailureCategory.RESOURCE_EXHAUSTION, Confidence.HIGH, _FIX_THE_CODE, f"the sandbox ended with {v.exec_status}", v.reason, varying=v.reason, exact=(v.exec_status,))
    raised = _exceptions(tail)
    if kind == "SYNTAX_ERROR":
        syntax = [f"{name}: {message}" for name, message in raised if name in ("SyntaxError", "IndentationError", "TabError")]
        return found(FailureCategory.SYNTAX_ERROR, Confidence.HIGH, _FIX_THE_CODE, *syntax[:2], varying=" ".join(syntax))
    if kind == "IMPORT_ERROR":
        modules = _MODULE.findall(tail)
        imports = [f"{name}: {message}" for name, message in raised if name in ("ImportError", "ModuleNotFoundError")]
        return found(
            FailureCategory.MISSING_DEPENDENCY, Confidence.HIGH if modules else Confidence.MEDIUM, _FIX_THE_CODE, *imports[:2], "only the standard library is available, and nothing can be installed",
            varying=" ".join(imports), exact=tuple(sorted(set(modules))),
        )  # fmt: skip
    if kind in ("NO_TESTS", "MISSING_CRITERION_TEST"):
        return found(FailureCategory.INVALID_CODE, Confidence.HIGH, _FIX_THE_CODE, v.reason, varying=v.reason, exact=(kind,))
    failed = tuple(sorted(v.counts.failed_names))
    if not v.counts.readable:
        return found(FailureCategory.UNKNOWN, Confidence.LOW, (Action.REPAIR, Action.STOP), v.reason, *(f"{n}: {m}" for n, m in raised[-2:]), varying=" ".join(n for n, _ in raised))
    assertions = [f"{name}: {message}" for name, message in raised if name == "AssertionError"]
    others = [f"{name}: {message}" for name, message in raised if name != "AssertionError"]
    if v.counts.failures:
        return found(FailureCategory.TEST_FAILURE, Confidence.HIGH, _FIX_THE_CODE, v.reason, *assertions[:3], varying=" ".join(assertions), exact=failed)
    if v.counts.errors:
        return found(FailureCategory.RUNTIME_EXCEPTION, Confidence.HIGH if others else Confidence.MEDIUM, _FIX_THE_CODE, v.reason, *others[:3], varying=" ".join(others), exact=failed)
    return found(FailureCategory.UNKNOWN, Confidence.LOW, (Action.REPAIR, Action.STOP), v.reason, varying=v.reason)


def classify_attempt(signal: AttemptSignal) -> Diagnosis:
    """A coding attempt that was not kept. Safety facts first: a provider's detail text is only evidence."""
    kind = f"CODING_{signal.status.value}"

    def found(category: FailureCategory, confidence: Confidence, actions: tuple[Action, ...], *evidence: str) -> Diagnosis:
        # Known by task and status: a provider's message can name this attempt's own container, so its wording changes every time.
        return Diagnosis(category, kind, CODING, confidence, _lines(*evidence), actions, fingerprint(category, str(signal.task), signal.status.value))

    only_shape = bool(signal.violations) and all(_SHAPE_VIOLATION.search(v) for v in signal.violations)
    if not only_shape and (signal.status in _REFUSED_ACCESS or signal.violations):
        return found(FailureCategory.SECURITY_VIOLATION, Confidence.HIGH, _ONLY_STOP, *signal.violations, signal.detail, f"the attempt ended {signal.phase}")
    if signal.phase not in _PROVEN_UNDONE:
        return found(FailureCategory.AMBIGUOUS_STATE, Confidence.HIGH, _ONLY_STOP, f"the attempt ended {signal.phase}, which does not prove it was undone", signal.detail)
    if signal.status is CodingStatus.PROTOCOL:
        return found(FailureCategory.AMBIGUOUS_STATE, Confidence.HIGH, _ONLY_STOP, "the engine answered with something DevPilot cannot interpret", signal.detail)
    if only_shape:
        return found(FailureCategory.INVALID_CODE, Confidence.HIGH, (Action.RETRY, Action.SWITCH_TOOL, Action.STOP), *signal.violations)
    if signal.status is CodingStatus.TIMEOUT:
        return found(FailureCategory.TIMEOUT, Confidence.HIGH, _NEEDS_TIME, signal.detail)
    if signal.status is CodingStatus.LIMIT:
        return found(FailureCategory.RESOURCE_EXHAUSTION, Confidence.HIGH, _ONLY_ANOTHER_TOOL, signal.detail)
    if signal.status is CodingStatus.UNAVAILABLE:
        return found(FailureCategory.TOOL_UNAVAILABLE, Confidence.HIGH, _TRY_ELSEWHERE, signal.detail)
    if signal.status in (CodingStatus.FAILED, CodingStatus.CANCELLED):
        return found(FailureCategory.TOOL_UNAVAILABLE, Confidence.MEDIUM, _TRY_ELSEWHERE, signal.detail)
    return found(FailureCategory.UNKNOWN, Confidence.LOW, (Action.RETRY, Action.STOP), f"the attempt ended {signal.status.value}", signal.detail)


def classify_refusal(excluded: Mapping[str, str]) -> Diagnosis:
    """No tool could even start. Nothing ran, so there is nothing to undo, and nothing changes by asking again."""
    reasons = tuple(f"{tool}: {why}" for tool, why in sorted(excluded.items()))
    category = FailureCategory.TOOL_UNAVAILABLE
    return Diagnosis(category, "NO_ELIGIBLE_TOOL", CODING, Confidence.HIGH, _lines(*reasons) or ("no tool is configured",), _ONLY_STOP, fingerprint(category, *sorted(excluded)))
