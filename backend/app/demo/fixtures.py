"""The files the offline demonstration and evaluation use, all written in this repository. No model wrote them.

The calculator, its tests and the subtract bug are the files `app.demo.mvp` ran in real Docker on 2026-10-31
(`D:\\DevPilotCache\\mvp-demo`). That is historical evidence about those files, not about this stage's code.

`TREES` lists every set of files a recording exists for (app/sandbox/replay.py). A run of any other files has
no recording and gets "needs the Docker sandbox".
"""

from __future__ import annotations

from app.demo.mvp import _SEED, BROKEN_CALCULATOR, CALCULATOR, CALCULATOR_TESTS
from app.verification.acceptance import AcceptanceSuite, SuiteCriterion, evaluation_files

SEED = dict(_SEED)  # the user's project before DevPilot runs

# Division by zero returns infinity, and the provider's own test agrees, so its tests pass: a self-graded pass.
LENIENT_CALCULATOR = CALCULATOR.replace('raise ZeroDivisionError("division by zero")', 'return float("inf")')
LENIENT_TESTS = CALCULATOR_TESTS.replace(
    "        with self.assertRaises(ZeroDivisionError):\n            divide(1, 0)\n", '        self.assertEqual(divide(1, 0), float("inf"))\n'
)
SYNTAX_CALCULATOR = CALCULATOR.replace("def subtract(a, b):", "def subtract(a, b)")  # a missing colon

ACCEPTANCE_TESTS = '''"""Acceptance tests for the calculator, written by the evaluator before any code exists."""

import unittest

import calculator


class CalculatorAcceptance(unittest.TestCase):
    def test_at_001_adds_two_numbers(self):
        self.assertEqual(calculator.add(-2, 7), 5)

    def test_at_002_subtracts_the_second_from_the_first(self):
        self.assertEqual(calculator.subtract(10, 4), 6)

    def test_at_003_multiplies_two_numbers(self):
        self.assertAlmostEqual(calculator.multiply(1.5, 4), 6.0)

    def test_at_004_divides_two_numbers(self):
        self.assertAlmostEqual(calculator.divide(7, 2), 3.5)

    def test_at_005_division_by_zero_is_an_error(self):
        with self.assertRaises(ZeroDivisionError):
            calculator.divide(5, 0)
'''

ACCEPTANCE_SUITE = AcceptanceSuite(
    criteria=(
        SuiteCriterion("AT-001", "add(a, b) returns the sum"),
        SuiteCriterion("AT-002", "subtract(a, b) returns a minus b"),
        SuiteCriterion("AT-003", "multiply(a, b) returns the product"),
        SuiteCriterion("AT-004", "divide(a, b) returns the quotient"),
        SuiteCriterion("AT-005", "divide(a, 0) raises ZeroDivisionError"),
    ),
    files={"test_acceptance.py": ACCEPTANCE_TESTS},
)


def _project(**files: str) -> dict[str, bytes]:
    return {rel: text.encode("utf-8") for rel, text in {**SEED, **files}.items()}


GOOD = _project(**{"calculator.py": CALCULATOR, "test_calculator.py": CALCULATOR_TESTS})
SUBTRACT_BUG = _project(**{"calculator.py": BROKEN_CALCULATOR, "test_calculator.py": CALCULATOR_TESTS})
LENIENT = _project(**{"calculator.py": LENIENT_CALCULATOR, "test_calculator.py": LENIENT_TESTS})
SYNTAX_ERROR = _project(**{"calculator.py": SYNTAX_CALCULATOR, "test_calculator.py": CALCULATOR_TESTS})

TREES: dict[str, dict[str, bytes]] = {
    "calculator: the provider's tests": GOOD,
    "calculator: the evaluator's acceptance suite": evaluation_files(GOOD, ACCEPTANCE_SUITE),
    "subtract bug: the provider's tests": SUBTRACT_BUG,
    "lenient division: the provider's tests": LENIENT,
    "lenient division: the evaluator's acceptance suite": evaluation_files(LENIENT, ACCEPTANCE_SUITE),
    "syntax error: the provider's tests": SYNTAX_ERROR,
}
