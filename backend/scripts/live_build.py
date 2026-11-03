"""The first real build: GPT plans, real OpenCode writes a small Python application, real Docker tests it, and an evaluator's
frozen suite judges it independently. One paid run, at most $0.50, within the experiments' cumulative total below.

    & $env:DEVPILOT_PYTHON scripts\\live_build.py                       # preflight only: contacts nothing external
    & $env:DEVPILOT_PYTHON scripts\\live_build.py --approve-spend 0.50  # APPROVAL: asks for a typed "second run", then spends up to that much

Run it from the backend folder, with Docker running. This is `scripts/live_demo.py`'s code with another experiment: the same
preflight, keep-awake, guard, free model-access check, dry launch, real containers and cleanup. Nothing is scripted: a run counts
as a success only if the real model made requests, nothing in it was simulated, the evaluator's suite passed, and no container or
network was left behind.

The $0.50 is per run. Across runs, a ledger beside the run folders counts every run at its whole cap from the moment it starts until it
is settled, so a crash cannot hide spending. The first experiment's $0.379808 stays in it; the second may add at most $0.50
(EXPERIMENT_BUDGET_USD), and a run is refused when it could pass that total.

The limits are constants in this file, so what was approved is what runs. `.env` supplies only the key, and both forms load it into
this process, the preflight too. The cap is strict: before every GPT request and every OpenCode request, the most it could cost is
worked out and the call is not sent if it could pass the run's limit. A run that stops at the cap is reported as stopped, not as
a success. Billing: costs are computed at $2 in / $10 out per million tokens; OpenAI's usage page is the authority.
"""

from __future__ import annotations

import ctypes
import secrets
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))  # live_demo.py, beside this file
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import live_demo  # noqa: E402
from app.plan_cli import Ledger  # noqa: E402
from app.verification.acceptance import AcceptanceSuite, SuiteCriterion, validate  # noqa: E402

SPEND_LIMIT_USD = 0.50  # one run, GPT and OpenCode together: the only figure --approve-spend accepts
# Across runs. The first experiment (2026-11-03) is in the ledger at $0.379808, settled, and stays there: the ledger is never reset.
# The second experiment may add at most $0.50 to it. A run starts only if what is counted plus a whole run's cap still fits, so
# after the second run has spent anything, a third is refused; one that stopped before any model call counted nothing and may be
# repeated. Committed with the user's approval on 2026-11-03. The paid run itself is approved separately, each time: the
# --approve-spend flag and the typed confirmation.
FIRST_EXPERIMENT_USD = 0.379808
SECOND_EXPERIMENT_USD = 0.50
EXPERIMENT_BUDGET_USD = round(FIRST_EXPERIMENT_USD + SECOND_EXPERIMENT_USD, 6)
CONFIRMATION = "second run"
NAME = "Student attendance tracker"
REQUEST = (
    "Create a Python student attendance tracker that can add students, record daily attendance and calculate attendance percentages. Include unit tests.\n"
    "\n"
    "Interface. Put the code in attendance.py, using only the Python standard library, with everything in memory:\n"
    "- class AttendanceTracker with no constructor arguments.\n"
    "- add_student(student_id: str, name: str) -> None. A blank student_id, or one already added, raises ValueError.\n"
    "- students() -> list[str]: the student IDs, in the order they were added.\n"
    "- record(student_id: str, day: str, present: bool) -> None. day is an ISO date, YYYY-MM-DD; an invalid date raises ValueError and records nothing. "
    "An unknown student_id raises KeyError. Recording the same student and day again replaces the earlier mark.\n"
    "- percentage(student_id: str) -> float | None: present marks divided by marks recorded, times 100, rounded to 2 decimal places. "
    "None when no mark was recorded. An unknown student_id raises KeyError.\n"
    "\n"
    "Put the unit tests in test_attendance.py, using unittest.\n"
    "\n"
    "The program is small: plan it as one coding task that writes attendance.py and test_attendance.py together."
)
# The one-task line is there because the first run's plan split the module from its tests: the second session paid OpenCode's base
# prompt again and read the first session's file, and each task can fail and be retried on its own. It changes nothing the evaluator
# checks: the frozen suite still judges the result, and the plan gate still requires every requirement to be covered.

ACCEPTANCE_TESTS = '''"""Acceptance tests for the attendance tracker, written by the evaluator before any code exists."""

import unittest

import attendance


def tracker():
    result = attendance.AttendanceTracker()
    result.add_student("S1", "Asha")
    result.add_student("S2", "Ben")
    return result


class AttendanceAcceptance(unittest.TestCase):
    def test_at_001_students_are_listed_in_the_order_added(self):
        self.assertEqual(tracker().students(), ["S1", "S2"])

    def test_at_002_a_duplicate_student_id_is_refused(self):
        t = tracker()
        with self.assertRaises(ValueError):
            t.add_student("S1", "Someone else")
        self.assertEqual(t.students(), ["S1", "S2"])

    def test_at_003_a_blank_student_id_is_refused(self):
        t = tracker()
        for blank in ("", "   "):
            with self.assertRaises(ValueError):
                t.add_student(blank, "Nobody")
        self.assertEqual(t.students(), ["S1", "S2"])

    def test_at_004_percentage_is_present_marks_over_marks_recorded(self):
        t = tracker()
        t.record("S1", "2026-11-02", True)
        t.record("S1", "2026-11-03", True)
        t.record("S1", "2026-11-04", False)
        self.assertEqual(t.percentage("S1"), 66.67)
        t.record("S2", "2026-11-02", False)
        self.assertEqual(t.percentage("S2"), 0.0)
        t.record("S2", "2026-11-03", True)
        t.record("S2", "2026-11-04", True)
        t.record("S2", "2026-11-05", True)
        t.record("S2", "2026-11-06", True)
        self.assertEqual(t.percentage("S2"), 80.0)

    def test_at_005_a_student_with_no_marks_has_no_percentage(self):
        self.assertIsNone(tracker().percentage("S2"))

    def test_at_006_recording_the_same_day_again_replaces_the_mark(self):
        t = tracker()
        t.record("S1", "2026-11-02", False)
        t.record("S1", "2026-11-02", True)
        self.assertEqual(t.percentage("S1"), 100.0)
        t.record("S1", "2026-11-03", False)
        self.assertEqual(t.percentage("S1"), 50.0)

    def test_at_007_an_unknown_student_is_an_error(self):
        t = tracker()
        with self.assertRaises(KeyError):
            t.record("S9", "2026-11-02", True)
        with self.assertRaises(KeyError):
            t.percentage("S9")

    def test_at_008_an_invalid_date_is_refused_and_nothing_is_recorded(self):
        t = tracker()
        for bad in ("2026-13-40", "yesterday", "", "2026-11-31"):
            with self.assertRaises(ValueError):
                t.record("S1", bad, True)
        self.assertIsNone(t.percentage("S1"))
'''

SUITE = AcceptanceSuite(
    criteria=(
        SuiteCriterion("AT-001", "students() lists the IDs in the order they were added"),
        SuiteCriterion("AT-002", "a duplicate student ID raises ValueError and changes nothing"),
        SuiteCriterion("AT-003", "a blank student ID raises ValueError"),
        SuiteCriterion("AT-004", "percentage is present marks over marks recorded, times 100, rounded to 2 places"),
        SuiteCriterion("AT-005", "a student with no marks has percentage None"),
        SuiteCriterion("AT-006", "recording the same student and day again replaces the earlier mark"),
        SuiteCriterion("AT-007", "an unknown student ID raises KeyError in record and percentage"),
        SuiteCriterion("AT-008", "an invalid date raises ValueError and records nothing"),
    ),
    files={"test_acceptance.py": ACCEPTANCE_TESTS},
)
validate(SUITE)
ACCEPTANCE: dict[str, Any] = SUITE.to_dict()

# live_demo's approved limits, tightened for a $0.50 run. The run budget is shared by GPT and OpenCode, and each attempt gets the
# smaller of GATEWAY_MAX_USD_PER_TASK and what the run has left. A request whose worst case could pass a limit is not sent.
#
# The token limits are NOT tighter than the dollars allow. The first real run (2026-11-03) set them to 80,000 an attempt and 150,000 a
# run, and three of its four attempts were refused by the gateway's own token budget, after about $0.27 of work that was then rolled
# back: the gateway reserves each request at its size in bytes (about five times its real tokens), so a limit of that size ran out after
# 3 to 6 requests while only $0.10 of the $0.50 was spent. At $2 per million tokens, $0.50 buys at most about 250,000 tokens, so limits at
# or above these can never bind before the dollars do, and the dollars are the limit that matters.
LIMITS: dict[str, Any] = {
    **live_demo.LIMITS,
    "run_budget_usd": SPEND_LIMIT_USD, "openai_max_output_tokens": 3_000, "run_max_tokens": 300_000, "max_turns": 12, "max_tool_calls": 12,
    "gateway_max_output_tokens": 4_000, "gateway_max_usd_per_task": 0.30, "opencode_max_tokens_per_task": 200_000,
}  # fmt: skip

FLOORS = {"c_free_gb": 10.0, "ram_available_gb": 4.0, "commit_max_percent": 70.0}


def machine_readings() -> dict[str, float | None]:
    """C: free space, available RAM and committed memory. Read-only; None where this platform cannot say."""
    readings: dict[str, float | None] = {"c_free_gb": None, "ram_available_gb": None, "commit_percent": None}
    try:
        readings["c_free_gb"] = shutil.disk_usage("C:\\").free / 1024**3
    except OSError:
        pass
    if sys.platform != "win32":
        return readings

    class Status(ctypes.Structure):
        _fields_ = (("length", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total_phys", ctypes.c_ulonglong), ("avail_phys", ctypes.c_ulonglong), ("total_commit", ctypes.c_ulonglong),
                    ("avail_commit", ctypes.c_ulonglong), ("total_virtual", ctypes.c_ulonglong), ("avail_virtual", ctypes.c_ulonglong), ("avail_extended", ctypes.c_ulonglong))  # fmt: skip

    status = Status()
    status.length = ctypes.sizeof(Status)
    if ctypes.WinDLL("kernel32").GlobalMemoryStatusEx(ctypes.byref(status)) and status.total_commit:
        readings["ram_available_gb"] = status.avail_phys / 1024**3
        readings["commit_percent"] = 100 * (status.total_commit - status.avail_commit) / status.total_commit
    return readings


def machine_checks(read: Callable[[], dict[str, float | None]] = machine_readings) -> list[tuple[str, bool, str]]:
    """The laptop's headroom for Docker, as the readiness document sets it (AC power is checked by live_demo's preflight)."""
    now = read()
    free, ram, commit = now["c_free_gb"], now["ram_available_gb"], now["commit_percent"]
    return [
        (f"C: has at least {FLOORS['c_free_gb']:g} GB free", free is not None and free >= FLOORS["c_free_gb"], "unreadable" if free is None else f"{free:.2f} GB"),
        (f"at least {FLOORS['ram_available_gb']:g} GB of RAM is available", ram is not None and ram >= FLOORS["ram_available_gb"], "unreadable" if ram is None else f"{ram:.2f} GB"),
        (f"committed memory is at most {FLOORS['commit_max_percent']:g}%", commit is not None and commit <= FLOORS["commit_max_percent"], "unreadable" if commit is None else f"{commit:.0f}%"),
    ]  # fmt: skip


class BuildSpend:
    """Spending across runs of this script, so EXPERIMENT_BUDGET_USD is a cumulative total and not a per-run figure. A run counts at its whole
    cap from the moment it starts until it is settled, so a crash or a closed terminal cannot hide it. A run that stopped before any
    model call settles at zero. An unreadable ledger refuses: nothing is started without knowing what was spent."""

    def __init__(self, path: Callable[[], Path] | None = None) -> None:
        self._path = path or (lambda: live_demo.DEMO_ROOT / "build-ledger.jsonl")
        self._run: str | None = None

    def counted(self) -> float | None:
        """What the ledger counts so far, or None when it cannot be read."""
        try:
            return Ledger(self._path()).spent()
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def check(self) -> list[tuple[str, bool, str]]:
        label = f"a whole run (${SPEND_LIMIT_USD:.2f}) still fits the ${EXPERIMENT_BUDGET_USD:.4f} the experiments may spend in all"
        spent = self.counted()
        if spent is None:
            return [(label, False, f"the ledger {self._path()} cannot be read")]
        return [(label, spent + SPEND_LIMIT_USD <= EXPERIMENT_BUDGET_USD + 1e-9, f"${spent:.4f} already counted (ledger: {self._path()})")]

    def start(self) -> None:
        self._run = secrets.token_hex(8)
        Ledger(self._path()).reserve(self._run, SPEND_LIMIT_USD)

    def end(self, spent: float | None) -> None:
        if self._run is not None:
            Ledger(self._path()).settle(self._run, SPEND_LIMIT_USD if spent is None else max(spent, 0.0))
            self._run = None


SPEND = BuildSpend()


def confirm(ask: Callable[[str], str] = input, say: Callable[[str], None] = print) -> bool:
    """A person's own go-ahead, in the terminal. Anything but the typed phrase stops the run before Docker or OpenAI are touched.
    The phrase is not "yes", so a habit from the first experiment cannot start the second."""
    limits = LIMITS
    counted = SPEND.counted()
    say(
        f"\nThis starts a PAID run, the SECOND experiment: {live_demo.MODEL} plans, real OpenCode writes the code, and real Docker containers test it.\n"
        f"  Already counted in the ledger: {'unreadable' if counted is None else f'${counted:.4f}'} (the first experiment's ${FIRST_EXPERIMENT_USD:.4f} included).\n"
        f"  This run adds at most ${SPEND_LIMIT_USD:.2f}, and the experiments together stay within ${EXPERIMENT_BUDGET_USD:.4f}.\n"
        f"  Total cap: ${SPEND_LIMIT_USD:.2f} across GPT and OpenCode, enforced before every request (a request that could pass it is not sent).\n"
        f"  GPT: at most {limits['max_turns']} turns, {limits['openai_max_output_tokens']:,} output tokens each, no retries.\n"
        f"  OpenCode: each attempt capped at ${limits['gateway_max_usd_per_task']:.2f} or what is left, {limits['gateway_max_requests_per_task']} requests, "
        f"{limits['opencode_max_attempts_per_task']} attempts, {limits['max_repair_attempts']} repair.\n"
        f"  Time: {limits['run_wall_clock_seconds']:,.0f} s. Docker networks are created for each attempt and removed afterwards.\n"
        "  A run that stops at the cap is reported as stopped, not as a success. Costs use $2 in / $10 out per million tokens; OpenAI's usage page is the authority.\n"
        "  Nothing is retried at a cost by itself: a refused or rate-limited attempt stops the run, and a new run needs this question again."
    )
    return ask(f'Type "{CONFIRMATION}" to start: ').strip().lower() == CONFIRMATION


def judge(summary: dict[str, Any]) -> list[str]:
    """Why a finished run is not the genuine success this experiment is for. Empty means it is."""
    problems: list[str] = []
    report = summary.get("report") or {}
    if summary.get("attempts", 0) < 1:
        problems.append("OpenCode made no real model request, so nothing was generated by a model")
    simulated = (report.get("provenance") or {}).get("simulated")
    if simulated is None or simulated:
        problems.append(f"something in the run was simulated or unknown: {simulated}")
    acceptance = report.get("acceptance") or {}
    if acceptance.get("verdict") != "PASSED":
        problems.append(f"the evaluator's acceptance suite did not pass (verdict: {acceptance.get('verdict')}: {acceptance.get('reason')})")
    delivered = {f["path"] for f in report.get("delivered_files", [])}
    if "attendance.py" not in delivered:
        problems.append("attendance.py was not delivered")
    cost = summary.get("cost_usd")
    if cost is None or cost > SPEND_LIMIT_USD:
        problems.append(f"the run's cost is {cost}, not within ${SPEND_LIMIT_USD:.2f}")
    return problems


EXPERIMENT = live_demo.Experiment(
    name=NAME, request=REQUEST, limits=LIMITS, spend_limit=SPEND_LIMIT_USD, acceptance=ACCEPTANCE, extra_checks=lambda: machine_checks() + SPEND.check(), confirm=confirm, judge=judge,
    show_report=True, on_start=SPEND.start, on_end=SPEND.end,
)


def main(argv: list[str] | None = None) -> int:
    return live_demo.main(argv, EXPERIMENT, doc=__doc__)


if __name__ == "__main__":
    sys.exit(main())
