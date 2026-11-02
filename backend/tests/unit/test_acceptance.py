"""Independent acceptance on its own: the suite's checks, its freezing, its folder, and what each reader may see."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.demo.fixtures import ACCEPTANCE_SUITE, GOOD, LENIENT
from app.recovery.diagnosis import FailureCategory, classify_verification
from app.sandbox.replay import ReplaySandbox, recording_key
from app.sandbox.spec import ExecResult, ExecSpec, ExecStatus
from app.verification import acceptance, recipes
from app.verification.acceptance import AcceptanceSuite, SuiteCriterion, SuiteError
from app.verification.results import Verdict, criterion_pattern

RECORDINGS = json.loads((Path(acceptance.__file__).resolve().parents[1] / "demo" / "recordings.json").read_text(encoding="utf-8"))["recordings"]


def recorded(name: str) -> ExecResult:
    r = next(r for r in RECORDINGS if r["name"] == name)
    return ExecResult(ExecStatus(r["status"]), "python-unittest", "c", r["exit_code"], stdout=r["stdout"], stderr=r["stderr"], cleanup="removed")


# --- The suite's own checks ----------------------------------------------------------------------------------------


def test_the_demonstration_suite_is_valid() -> None:
    acceptance.validate(ACCEPTANCE_SUITE)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda s: replace(s, criteria=(*s.criteria, SuiteCriterion("AT-006", "not tested"))), "no test names AT-006"),
        (lambda s: replace(s, criteria=(SuiteCriterion("AC-001", "a developer code"),)), "look like AT-001"),
        (lambda s: replace(s, criteria=(s.criteria[0], s.criteria[0])), "unique"),
        (lambda s: replace(s, files={"tests/test_x.py": "def test_at_001(): pass"}), "no folder"),
        (lambda s: replace(s, files={"helper.py": "def test_at_001(): pass"}), "test_<name>.py"),
        (lambda s: replace(s, files={"test_x.py": "def test_at_001(:"}), "not valid Python"),
        (lambda s: replace(s, criteria=(replace(s.criteria[0], priority="COULD"),)), "MUST or SHOULD"),
    ],
)
def test_a_suite_that_cannot_prove_its_criteria_is_refused(change: object, message: str) -> None:
    with pytest.raises(SuiteError, match=message):
        acceptance.validate(change(ACCEPTANCE_SUITE))  # type: ignore[operator]


def test_checking_a_suite_never_runs_it(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    source = f"open({str(marker)!r}, 'w').write('x')\n" + "".join(f"def test_at_00{n}(): pass\n" for n in range(1, 6))
    acceptance.validate(replace(ACCEPTANCE_SUITE, files={"test_x.py": source}))
    assert not marker.exists()


def test_a_frozen_suite_comes_back_unchanged_and_a_changed_copy_is_refused() -> None:
    text, sha = acceptance.freeze(ACCEPTANCE_SUITE)
    assert acceptance.thaw(text, sha) == AcceptanceSuite.from_dict(ACCEPTANCE_SUITE.to_dict())
    with pytest.raises(SuiteError, match="no longer matches"):
        acceptance.thaw(text.replace("assertRaises", "assertTrue"), sha)


def test_freezing_is_deterministic() -> None:
    assert acceptance.freeze(ACCEPTANCE_SUITE) == acceptance.freeze(AcceptanceSuite.from_dict(json.loads(json.dumps(ACCEPTANCE_SUITE.to_dict()))))


# --- The folder the evaluator's tests run in ------------------------------------------------------------------------


def test_the_providers_tests_are_left_out_and_the_evaluators_go_in_last() -> None:
    candidate = {"calculator.py": b"x", "test_calculator.py": b"t", "tests.py": b"t", "pkg/test_inner.py": b"t", "test_acceptance.py": b"forged", "README.md": b"r"}
    files = acceptance.evaluation_files(candidate, ACCEPTANCE_SUITE)
    assert set(files) == {"calculator.py", "README.md", "test_acceptance.py"}
    assert files["test_acceptance.py"] == ACCEPTANCE_SUITE.files["test_acceptance.py"].encode()


def test_the_folder_is_made_fresh_and_removed(tmp_path: Path) -> None:
    from app.tools.adapters.workspace_fs import WorkspaceFS

    fs = WorkspaceFS(tmp_path / "acceptance", max_file_bytes=100_000, max_files=50)
    root = acceptance.stage(fs, "a" * 32, {"stale.py": b"x"})
    root = acceptance.stage(fs, "a" * 32, {"calculator.py": b"y"})
    assert sorted(p.name for p in root.iterdir()) == ["calculator.py"]
    acceptance.discard(fs, "a" * 32)
    assert not root.exists()


# --- Judging -------------------------------------------------------------------------------------------------------


def test_the_real_calculator_passes_every_criterion() -> None:
    result = acceptance.judge_acceptance(recorded("calculator: the evaluator's acceptance suite"), ACCEPTANCE_SUITE, "sha")
    assert result.verdict == acceptance.PASSED and result.passed == 5 and all(c.status == "PASSED" for c in result.criteria)


def test_a_self_graded_pass_fails_the_criterion_it_dodged_and_partial_completion_is_reported() -> None:
    result = acceptance.judge_acceptance(recorded("lenient division: the evaluator's acceptance suite"), ACCEPTANCE_SUITE, "sha")
    data = result.to_dict()
    assert result.verdict == acceptance.FAILED and result.passed == 4 and data["partial"] is True and data["total"] == 5
    failed = [c for c in result.criteria if c.status != "PASSED"]
    assert [c.code for c in failed] == ["AT-005"] and "ZeroDivisionError not raised" in failed[0].evidence and "AT-005" in result.reason


def test_a_failing_should_criterion_is_reported_but_does_not_fail_acceptance() -> None:
    relaxed = replace(ACCEPTANCE_SUITE, criteria=(*ACCEPTANCE_SUITE.criteria[:4], replace(ACCEPTANCE_SUITE.criteria[4], priority="SHOULD")))
    result = acceptance.judge_acceptance(recorded("lenient division: the evaluator's acceptance suite"), relaxed, "sha")
    assert result.verdict == acceptance.PASSED and result.passed == 4 and "MUST" in result.reason


def test_a_sandbox_with_no_answer_says_nothing_about_the_code() -> None:
    spec = ExecSpec("python-unittest", "img", ("python",), Path("."), 10.0, "c")
    result = acceptance.judge_acceptance(ExecResult.unavailable(spec, "docker is down"), ACCEPTANCE_SUITE, "sha")
    assert result.verdict == acceptance.NOT_RUN and result.passed == 0


# --- What each reader may see --------------------------------------------------------------------------------------


def test_recovery_and_gpt_see_the_unmet_criteria_and_nothing_of_the_tests() -> None:
    result = acceptance.judge_acceptance(recorded("lenient division: the evaluator's acceptance suite"), ACCEPTANCE_SUITE, "sha")
    seen = acceptance.for_recovery(result, "digest")
    shown = json.dumps([seen.to_dict(), result.for_gpt()])
    assert seen.verdict is Verdict.FAILED and seen.counts.failed_names == ("AT-005",) and seen.output_tail == "" and seen.fingerprint == "digest"
    assert "test_at_" not in shown and "assertRaises" not in shown and "not raised" not in shown and "Traceback" not in shown
    assert "divide(a, 0) raises ZeroDivisionError" in seen.reason
    assert classify_verification(seen).category is FailureCategory.TEST_FAILURE


def test_an_acceptance_run_with_no_answer_is_not_a_code_failure() -> None:
    spec = ExecSpec("python-unittest", "img", ("python",), Path("."), 10.0, "c")
    seen = acceptance.for_recovery(acceptance.judge_acceptance(ExecResult.unavailable(spec, "down"), ACCEPTANCE_SUITE, "sha"), "d")
    assert seen.verdict is Verdict.NOT_RUN and classify_verification(seen).category is FailureCategory.TOOL_UNAVAILABLE


def test_an_acceptance_criterion_needs_its_own_prefix_and_developer_criteria_are_unchanged() -> None:
    assert criterion_pattern("AT-002").search("test_at_002_subtracts") and not criterion_pattern("AT-002").search("test_ac_002_subtracts")
    assert criterion_pattern("AC-002").search("test_ac_002_x") and criterion_pattern("AC-002").search("test_AC002") and not criterion_pattern("AC-002").search("test_ac_0021")


def test_the_recordings_used_here_are_for_the_fixture_trees() -> None:
    sandbox = ReplaySandbox.load()
    assert len(sandbox._recordings) == len(RECORDINGS)
    keys = {r["key"] for r in RECORDINGS}
    assert recording_key("python-unittest", ("python", "-I", "-B", "-m", "unittest", "discover", "-v", "-s", ".", "-p", "test*.py"), acceptance.evaluation_files(GOOD, ACCEPTANCE_SUITE)) in keys
    assert recording_key("python-unittest", ("python", "-I", "-B", "-m", "unittest", "discover", "-v", "-s", ".", "-p", "test*.py"), LENIENT) in keys


# --- Criteria bound to a front-end component ------------------------------------------------------------------------

FRONT_FILE = "frontend/__acceptance__/dashboard.test.jsx"
FRONT_SOURCE = "import { render } from '@testing-library/react';\ntest('AT-006 shows the reading', () => {});\nit(\"AT-007 shows an error state\", () => {});\n"
FRONT = replace(
    ACCEPTANCE_SUITE,
    criteria=(*ACCEPTANCE_SUITE.criteria, SuiteCriterion("AT-006", "the dashboard shows the reading", "MUST", "frontend"), SuiteCriterion("AT-007", "the dashboard shows an error state", "SHOULD", "frontend")),
    files={**ACCEPTANCE_SUITE.files, FRONT_FILE: FRONT_SOURCE},
)  # fmt: skip
REACT = recipes.RECIPES["react-vite"]


def vitest(*cases: tuple[str, str]) -> ExecResult:
    failed = sum(status == "failed" for _, status in cases)
    data = {"numTotalTests": len(cases), "numPassedTests": len(cases) - failed, "numFailedTests": failed, "numPendingTests": 0,
            "testResults": [{"assertionResults": [{"fullName": title, "status": status} for title, status in cases]}]}  # fmt: skip
    return ExecResult(ExecStatus.FAILED if failed else ExecStatus.SUCCEEDED, "react-acceptance", "c2", 1 if failed else 0, stdout=json.dumps(data), cleanup="removed")


def calculator() -> ExecResult:
    return recorded("calculator: the evaluator's acceptance suite")


def test_a_suite_may_bind_criteria_to_a_front_end_component() -> None:
    acceptance.validate(FRONT)
    assert FRONT.components() == ("frontend",) and FRONT.has_python


def _rebound(code: str, component: str | None) -> AcceptanceSuite:
    return replace(FRONT, criteria=tuple(replace(c, component=component) if c.code == code else c for c in FRONT.criteria))


@pytest.mark.parametrize(
    ("suite", "message"),
    [
        (_rebound("AT-006", "backend"), "no test names AT-006"),  # its files are frontend's
        (_rebound("AT-006", None), "no test names AT-006"),  # a Python criterion: front-end titles do not count
        (_rebound("AT-001", "frontend"), "no test names AT-001"),  # nor do Python test names for a front-end one
        (replace(FRONT, files={**ACCEPTANCE_SUITE.files, FRONT_FILE: FRONT_SOURCE.replace("test('AT-006", "test.skip('AT-006")}), "no test names AT-006"),
        (_rebound("AT-006", "Frontend"), "named like"),
        (_rebound("AT-006", "../frontend"), "named like"),
        (replace(FRONT, files={**ACCEPTANCE_SUITE.files, "frontend/dashboard.test.jsx": FRONT_SOURCE}), "__acceptance__"),
        (replace(FRONT, files={**ACCEPTANCE_SUITE.files, "frontend/__acceptance__/x.spec.jsx": FRONT_SOURCE}), "__acceptance__"),
        (replace(FRONT, files={**ACCEPTANCE_SUITE.files, "../frontend/__acceptance__/x.test.jsx": FRONT_SOURCE}), "__acceptance__"),
    ],
)
def test_a_component_criterion_is_proved_only_by_its_own_components_files(suite: AcceptanceSuite, message: str) -> None:
    with pytest.raises(SuiteError, match=message):
        acceptance.validate(suite)


def test_a_suite_without_components_freezes_to_exactly_the_text_it_had_before() -> None:
    text, _ = acceptance.freeze(ACCEPTANCE_SUITE)
    before = {"criteria": [{"code": c.code, "description": c.description, "priority": c.priority} for c in ACCEPTANCE_SUITE.criteria], "files": dict(sorted(ACCEPTANCE_SUITE.files.items()))}
    assert text == json.dumps(before, sort_keys=True, separators=(",", ":"), ensure_ascii=False) and '"component"' not in text


def test_a_components_binding_is_frozen_with_the_suite_and_a_changed_binding_is_refused() -> None:
    text, sha = acceptance.freeze(FRONT)
    assert acceptance.thaw(text, sha) == AcceptanceSuite.from_dict(FRONT.to_dict()) and acceptance.thaw(text, sha).components() == ("frontend",)
    with pytest.raises(SuiteError, match="no longer matches"):
        acceptance.thaw(text.replace('"component":"frontend"', '"component":"backend"', 1), sha)


def test_the_providers_front_end_tests_never_join_or_replace_the_evaluators() -> None:
    candidate = {"frontend/src/App.jsx": b"app", "frontend/src/App.test.jsx": b"t", "frontend/src/a.spec.ts": b"t", FRONT_FILE: b"forged", "frontend/__acceptance__/extra.test.jsx": b"forged",
                 "frontend/__acceptance__/helper.js": b"forged", "backend/app/main.py": b"m", "backend/tests/test_api.py": b"t"}  # fmt: skip
    files = acceptance.evaluation_files(candidate, FRONT)
    assert set(files) == {"frontend/src/App.jsx", "backend/app/main.py", "test_acceptance.py", FRONT_FILE} and files[FRONT_FILE] == FRONT_SOURCE.encode()


def test_front_end_criteria_are_never_passed_because_their_files_exist() -> None:
    spec = ExecSpec("react-acceptance", "img", ("node",), Path("."), 10.0, "c2")
    for components in (None, {}, {"frontend": (REACT, ExecResult.unavailable(spec, "the React runtime is not approved"))}):
        result = acceptance.judge_acceptance(calculator(), FRONT, "sha", components)
        assert result.verdict == acceptance.NOT_RUN and result.passed == 5 and {c.code: c.status for c in result.criteria}["AT-006"] == "UNVERIFIED"
    silent = acceptance.judge_acceptance(calculator(), FRONT, "sha", {"frontend": (REACT, vitest(("Dashboard renders", "passed")))})
    assert silent.verdict == acceptance.FAILED and "AT-006" in silent.reason and {c.code: c.status for c in silent.criteria}["AT-006"] == "UNVERIFIED"


def test_front_end_criteria_pass_only_in_their_own_run_and_each_run_is_reported_separately() -> None:
    result = acceptance.judge_acceptance(calculator(), FRONT, "sha", {"frontend": (REACT, vitest(("Dashboard AT-006 shows the reading", "passed"), ("Dashboard AT-007 shows an error state", "failed")))})
    data = result.to_dict()
    assert result.verdict == acceptance.PASSED and result.passed == 6  # AT-007 is a SHOULD
    assert [(p["component"], p["verdict"], p["criteria"]) for p in data["parts"]] == [("project", "PASSED", ["AT-001", "AT-002", "AT-003", "AT-004", "AT-005"]), ("frontend", "FAILED", ["AT-006", "AT-007"])]
    assert data["criteria"][5]["component"] == "frontend" and "component" not in data["criteria"][0]
    failing = acceptance.judge_acceptance(calculator(), FRONT, "sha", {"frontend": (REACT, vitest(("Dashboard AT-006 shows the reading", "failed")))})
    assert failing.verdict == acceptance.FAILED and failing.for_gpt()["unmet"] == [{"code": "AT-006", "description": "the dashboard shows the reading", "component": "frontend"}]
    assert "Dashboard" not in json.dumps(failing.for_gpt()) and acceptance.for_recovery(failing, "d").counts.failed_names == ("AT-006",)


def test_a_python_only_suite_is_judged_and_reported_as_before() -> None:
    data = acceptance.judge_acceptance(calculator(), ACCEPTANCE_SUITE, "sha").to_dict()
    assert "parts" not in data and all("component" not in c for c in data["criteria"]) and data["verdict"] == "PASSED"
