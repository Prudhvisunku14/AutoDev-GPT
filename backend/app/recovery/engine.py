"""Bounded recovery: what a failure means, and whether another attempt is allowed (ARCHITECTURE §9.2).

GPT chooses how to fix a failure. This module decides, in code, whether another try is allowed at all,
so a run can never loop on a failure. Each failure belongs to a class, each class has one action, and
the limits come from settings:

- REPAIR: the code is wrong. The run moves to REPAIRING and GPT asks for a fix.
- RETRY: the coding attempt failed and nothing was kept. The same task may run again.
- STOP: another try cannot help or is not allowed. The run ends, and says why.

This is the small rule table the MVP needs, not the full Phase 6 classifier.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

from app.coding.types import CodingStatus


class Action(StrEnum):
    REPAIR = "REPAIR"
    RETRY = "RETRY"
    STOP = "STOP"


REPAIRABLE = frozenset({"TEST_FAILURE", "SYNTAX_ERROR", "IMPORT_ERROR", "NO_TESTS", "MISSING_CRITERION_TEST", "TEST_TIMEOUT", "RESOURCE_LIMIT"})
# A retry starts a fresh attempt from a restored workspace, so it can help only when the failure is the
# engine's own. A policy breach, a refused credential or an unsafe server is not a matter of luck.
# CANCELLED reaches here only when the backend stopped: a run someone cancelled has already ended.
RETRYABLE_CODING = frozenset({CodingStatus.FAILED, CodingStatus.TIMEOUT, CodingStatus.UNAVAILABLE, CodingStatus.BUSY, CodingStatus.CANCELLED})

_HINTS = {
    "TEST_FAILURE": "Fix the code the failing tests exercise. Do not weaken or delete a test.",
    "SYNTAX_ERROR": "A file does not parse. Fix the syntax error shown in the output.",
    "IMPORT_ERROR": "A module cannot be imported. Fix the file name or the import; only the standard library is available.",
    "NO_TESTS": "No tests were found. Add unittest tests in files named test*.py.",
    "MISSING_CRITERION_TEST": "Add a test named after each unproved MUST criterion, such as test_ac_001_addition.",
    "TEST_TIMEOUT": "The tests did not finish in time. Look for a loop that never ends.",
    "RESOURCE_LIMIT": "The tests used too much memory or printed too much. Reduce it.",
}


@dataclass(frozen=True)
class Decision:
    failure_class: str
    action: Action
    reason: str
    attempts_used: int
    attempts_allowed: int
    hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "action": self.action.value}


def decide_verification(failure_class: str, *, repairs_used: int, max_repairs: int) -> Decision:
    """After a failed verification. `repairs_used` counts the repairs already taken."""
    if failure_class not in REPAIRABLE:
        return Decision(failure_class, Action.STOP, "the sandbox gave no answer about the code, so repairing it cannot help", repairs_used, max_repairs)
    if repairs_used >= max_repairs:
        return Decision(failure_class, Action.STOP, f"the repair limit of {max_repairs} is used up", repairs_used, max_repairs, _HINTS[failure_class])
    return Decision(failure_class, Action.REPAIR, "the tests failed and a repair is still allowed", repairs_used, max_repairs, _HINTS[failure_class])


def decide_coding(status: CodingStatus, *, quarantined: bool, attempts_used: int, max_attempts: int) -> Decision:
    """After a coding attempt that did not end COMPLETED, or that the audit refused."""
    failure_class = f"CODING_{status.value}"
    if quarantined:
        return Decision(failure_class, Action.STOP, "the workspace is quarantined until a person reviews it", attempts_used, max_attempts)
    if status not in RETRYABLE_CODING:
        return Decision(failure_class, Action.STOP, "another attempt would meet the same refusal", attempts_used, max_attempts)
    if attempts_used >= max_attempts:
        return Decision(failure_class, Action.STOP, f"the limit of {max_attempts} attempts for this task is used up", attempts_used, max_attempts)
    return Decision(failure_class, Action.RETRY, "the attempt was undone and the task may run again", attempts_used, max_attempts,
                    "Call generate_code for the same task again.")  # fmt: skip
