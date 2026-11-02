"""Independent acceptance: a frozen test suite the coding provider never sees, run against what it produced.

Tests written by the coding provider can only show that the code does what that provider meant. This suite
comes from someone else, and is fixed before any code exists:

- The evaluator (the user, or a judge) gives it with the requirement: acceptance criteria (AT-001, ...) and
  unittest files whose test names carry those codes. It is checked by parsing, and never executed here.
- Each run gets a copy and its SHA-256 when the run is created. A later change to the project cannot reach a
  running run, and a copy that no longer matches its hash stops the run.
- The coding provider never receives it: it is not in any workspace, brief or tool result. GPT learns only
  which criteria failed and what they say, never the tests or their output, so neither can write to the test.
- It runs in the sandbox, in a folder of its own: the candidate's files without the provider's test files,
  then the evaluator's files on top, so nothing the provider wrote can replace or join them.
- A criterion may be bound to one component of a multi-stack plan. It is then proved only by that component's front-end
  files (`<component>/__acceptance__/<name>.test.tsx`, checked by pattern, never run here), which run in that
  component's own runtime and are reported as a separate part. A criterion whose run did not happen is never PASSED,
  and a criterion the plan cannot run blocks verification instead of being skipped. **The React runtime does not exist,
  so front-end acceptance has never run on Docker.**

**Not an adversarial security boundary.** The evaluator's tests import the candidate's code into the same Python
process. Code written to subvert the test runner (patching `unittest`, say) could fake a pass. The suite catches
honest mistakes and self-graded passes; isolating each criterion in its own process would be needed against
deliberate cheating. The sandbox's own limits still contain the code either way.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from app.sandbox.spec import ExecResult, ExecStatus
from app.tools.adapters.workspace_fs import WorkspaceFS
from app.verification.recipes import FOLDER, judge_recipe
from app.verification.results import Criterion, CriterionStatus, TestCounts, Verdict, VerificationResult, criterion_pattern, judge, judge_criteria, title_name

CODE = re.compile(r"^AT-\d{3}$")
FILE_NAME = re.compile(r"^test_[A-Za-z0-9_]{1,60}\.py$")
FRONTEND_FILE = re.compile(r"^(?P<component>[a-z][a-z0-9_-]{0,31})/__acceptance__/[A-Za-z0-9_]{1,60}\.test\.(?:js|jsx|ts|tsx)$")
# A plain test("...") or it("...") call. A skipped, focused or computed title proves nothing, so it is not counted.
_TEST_TITLE = re.compile(r"""(?<![\w.$])(?:it|test)\(\s*(["'`])(?P<title>[^"'`\\\n]{1,200})\1""")
MAX_CRITERIA, MAX_FILES, MAX_FILE_CHARS = 30, 5, 64_000
# What `unittest discover -p test*.py` would find, every front-end test file, and anything in an __acceptance__ folder:
# none of the provider's tests may join or replace the evaluator's.
_DEVELOPER_TEST = re.compile(r"(^|/)test[^/]*\.py$|(^|/)[^/]+\.(?:test|spec)\.[cm]?[jt]sx?$|(^|/)__acceptance__/")
_EVIDENCE_CHARS = 600
PASSED, FAILED, NOT_RUN = "PASSED", "FAILED", "NOT_RUN"
PROJECT = "project"  # the part that runs the Python files


class SuiteError(ValueError):
    """The suite cannot be used. The message names what is wrong, and never echoes the tests."""


@dataclass(frozen=True)
class SuiteCriterion:
    code: str
    description: str
    priority: str = "MUST"
    component: str | None = None  # None: proved by the Python files; else only by that component's front-end files


def _without_absent_component(item: Mapping[str, Any]) -> dict[str, Any]:
    """A criterion with no component is written as before, so an existing suite freezes to the same text and hash."""
    return {k: v for k, v in item.items() if k != "component" or v is not None}


@dataclass(frozen=True)
class AcceptanceSuite:
    criteria: tuple[SuiteCriterion, ...]
    files: Mapping[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {"criteria": [_without_absent_component(c.__dict__) for c in self.criteria], "files": dict(sorted(self.files.items()))}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> AcceptanceSuite:
        return cls(tuple(SuiteCriterion(c["code"], c["description"], c.get("priority", "MUST"), c.get("component")) for c in data["criteria"]), dict(data["files"]))

    @property
    def has_python(self) -> bool:
        return any(c.component is None for c in self.criteria)

    def components(self) -> tuple[str, ...]:
        """The components whose own front-end files prove criteria."""
        return tuple(sorted({c.component for c in self.criteria if c.component is not None}))


def validate(suite: AcceptanceSuite) -> None:
    """Every criterion is proved by at least one test that names it. The files are parsed, never run."""
    codes = [c.code for c in suite.criteria]
    if not 1 <= len(codes) <= MAX_CRITERIA:
        raise SuiteError(f"a suite has 1 to {MAX_CRITERIA} criteria")
    if len(set(codes)) != len(codes) or not all(CODE.fullmatch(code) for code in codes):
        raise SuiteError("criterion codes must be unique and look like AT-001")
    if any(c.priority not in ("MUST", "SHOULD") for c in suite.criteria):
        raise SuiteError("a criterion's priority is MUST or SHOULD")
    if any(c.component is not None and not (isinstance(c.component, str) and FOLDER.fullmatch(c.component)) for c in suite.criteria):
        raise SuiteError("a criterion's component is named like the plan's folders, such as 'frontend'")
    if not 1 <= len(suite.files) <= MAX_FILES:
        raise SuiteError(f"a suite has 1 to {MAX_FILES} test files")
    tests: list[str] = []
    titles: dict[str, list[str]] = {}
    for name, source in suite.files.items():
        frontend = FRONTEND_FILE.fullmatch(name)
        if not frontend and not FILE_NAME.fullmatch(name):
            raise SuiteError(f"{name!r}: a suite file is named test_<name>.py, with no folder, or <component>/__acceptance__/<name>.test.ts (or .tsx, .js, .jsx)")
        if len(source) > MAX_FILE_CHARS:
            raise SuiteError(f"{name}: over {MAX_FILE_CHARS} characters")
        if frontend:  # read by pattern, never run
            titles.setdefault(frontend["component"], []).extend(title_name(m["title"]) for m in _TEST_TITLE.finditer(source))
            continue
        try:
            tree = ast.parse(source, filename=name)
        except SyntaxError as exc:
            raise SuiteError(f"{name}: not valid Python (line {exc.lineno})") from None
        tests += [node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith("test")]
    unproved = [c.code for c in suite.criteria if not any(criterion_pattern(c.code).search(t) for t in (tests if c.component is None else titles.get(c.component, [])))]
    if unproved:
        raise SuiteError(f"no test names {', '.join(unproved)}: a test proves AT-001 by holding at_001 in its name, in a file of the criterion's own component")


def freeze(suite: AcceptanceSuite) -> tuple[str, str]:
    """The canonical text and its SHA-256: what a run keeps."""
    text = json.dumps(suite.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()


def thaw(text: str, sha256: str) -> AcceptanceSuite:
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != sha256:
        raise SuiteError("the run's copy of the acceptance suite no longer matches the hash recorded when the run was created")
    return AcceptanceSuite.from_dict(json.loads(text))


def evaluation_files(candidate: Mapping[str, bytes], suite: AcceptanceSuite) -> dict[str, bytes]:
    """The candidate without the provider's tests, with the evaluator's files added last."""
    kept = {rel: data for rel, data in candidate.items() if not _DEVELOPER_TEST.search(rel) and PurePosixPath(rel).name not in suite.files}
    return {**kept, **{name: source.encode("utf-8") for name, source in suite.files.items()}}


def read_tree(root: Path) -> dict[str, bytes]:
    """Every regular file under `root`, by relative POSIX path. Links are not followed."""
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file() and not path.is_symlink()}


def stage(fs: WorkspaceFS, run_id: str, files: Mapping[str, bytes]) -> Path:
    """The folder the acceptance run sees, made fresh. `fs` is a jail like the workspaces."""
    discard(fs, run_id)
    root = fs.create_project(run_id)
    for rel, data in files.items():
        fs.write_bytes(run_id, rel, data)
    return root


def discard(fs: WorkspaceFS, run_id: str) -> None:
    if fs.has_project(run_id):
        shutil.rmtree(fs.project_dir(run_id), ignore_errors=True)


@dataclass(frozen=True)
class CriterionVerdict:
    code: str
    description: str
    priority: str
    status: str
    tests: tuple[str, ...] = ()
    evidence: str = ""  # the runner's own words about a failing test: for the user, never for GPT
    component: str | None = None


@dataclass(frozen=True)
class AcceptanceResult:
    verdict: str
    reason: str
    suite_sha256: str
    criteria: tuple[CriterionVerdict, ...]
    exec_status: str
    exit_code: int | None
    container: str
    duration_ms: int
    cleanup: str
    output_tail: str = ""
    notes: list[str] = field(default_factory=list)
    parts: tuple[dict[str, Any], ...] = ()  # one per run when front-end components have their own: reported separately

    @property
    def passed(self) -> int:
        return sum(c.status == CriterionStatus.PASSED.value for c in self.criteria)

    @property
    def unmet(self) -> list[CriterionVerdict]:
        return [c for c in self.criteria if c.priority == "MUST" and c.status != CriterionStatus.PASSED.value]

    def to_dict(self) -> dict[str, Any]:
        data = {
            "configured": True, "verdict": self.verdict, "reason": self.reason, "suite_sha256": self.suite_sha256, "passed": self.passed, "total": len(self.criteria),
            "partial": self.verdict == FAILED and self.passed > 0, "criteria": [_without_absent_component(c.__dict__ | {"tests": list(c.tests)}) for c in self.criteria],
            "exec_status": self.exec_status, "exit_code": self.exit_code, "container": self.container, "duration_ms": self.duration_ms, "cleanup": self.cleanup,
            "output_tail": self.output_tail,
        }  # fmt: skip
        return data | {"parts": [dict(p) for p in self.parts]} if self.parts else data

    def for_gpt(self) -> dict[str, Any]:
        """Which criteria failed and what they say. No test name, no test code and no output."""
        return {
            "verdict": self.verdict, "passed": self.passed, "total": len(self.criteria),
            "unmet": [_without_absent_component({"code": c.code, "description": c.description, "component": c.component}) for c in self.unmet],
            "note": "The acceptance tests are kept from the coding provider; only the criteria are shown.",
        }  # fmt: skip


def _part(name: str, v: VerificationResult, codes: list[str]) -> dict[str, Any]:
    return {"component": name, "verdict": v.verdict.value, "reason": v.reason, "criteria": codes, "exec_status": v.exec_status, "exit_code": v.exit_code,
            "container": v.container, "duration_ms": v.duration_ms, "cleanup": v.cleanup}  # fmt: skip


def judge_acceptance(result: ExecResult | None, suite: AcceptanceSuite, suite_sha256: str, components: Mapping[str, tuple[Any, ExecResult]] | None = None) -> AcceptanceResult:
    """PASSED needs every MUST criterion proved by a passing evaluator test, in the run of its own component. A SHOULD
    criterion is reported. `result` is the Python files' run; `components` maps each front-end component to its recipe
    and its run. A criterion whose run did not happen is NOT_RUN, never PASSED: its files existing proves nothing."""
    verdicts: dict[str, CriterionVerdict] = {}
    parts: list[tuple[str, VerificationResult, list[str]]] = []
    project = [c for c in suite.criteria if c.component is None]
    if project or result is not None:
        ran = result or ExecResult(ExecStatus.UNAVAILABLE, "python-unittest", "", detail="the acceptance suite did not run")
        judged = judge(ran, [Criterion(c.code, c.priority) for c in project])
        by_code = {c.code: c for c in judged.criteria}
        output = ran.stderr or ran.stdout
        for c in project:
            found = by_code.get(c.code)
            verdicts[c.code] = CriterionVerdict(c.code, c.description, c.priority, found.status.value if found else CriterionStatus.UNVERIFIED.value, found.tests if found else (),
                                                _evidence(output, found.tests) if found and found.status is CriterionStatus.FAILED else "")  # fmt: skip
        parts.append((PROJECT, judged, [c.code for c in project]))
    for name in suite.components():
        own = [c for c in suite.criteria if c.component == name]
        recipe, ran = (components or {}).get(name, (None, None))
        if recipe is None or ran is None:
            judged = VerificationResult(Verdict.NOT_RUN, "its acceptance run did not happen", ExecStatus.UNAVAILABLE.value, None, TestCounts(readable=False), (), "", "", 0, "not_needed")
        else:
            judged = judge_recipe(recipe, ran)
        by_code = {j.code: j for j in judge_criteria([Criterion(c.code, c.priority) for c in own], judged.counts)} if judged.verdict is not Verdict.NOT_RUN else {}
        for c in own:
            found = by_code.get(c.code)
            verdicts[c.code] = CriterionVerdict(c.code, c.description, c.priority, found.status.value if found else CriterionStatus.UNVERIFIED.value, found.tests if found else (), "", name)
        parts.append((name, judged, [c.code for c in own]))
    criteria = tuple(verdicts[c.code] for c in suite.criteria)
    lead = parts[0][1]
    base = {"suite_sha256": suite_sha256, "criteria": criteria, "exec_status": lead.exec_status, "exit_code": lead.exit_code, "container": lead.container,
            "duration_ms": sum(v.duration_ms for _, v, _ in parts), "cleanup": lead.cleanup, "output_tail": lead.output_tail,
            "parts": tuple(_part(name, v, codes) for name, v, codes in parts) if suite.components() else ()}  # fmt: skip
    silent = next(((v.reason if name == PROJECT else f"{name}: {v.reason}") for name, v, _ in parts if v.verdict is Verdict.NOT_RUN), None)
    if silent is not None:
        return AcceptanceResult(NOT_RUN, silent, **base)
    unmet = [c for c in criteria if c.priority == "MUST" and c.status != CriterionStatus.PASSED.value]
    if not unmet:
        return AcceptanceResult(PASSED, f"all {len(criteria)} acceptance criteria passed" if all(c.status == CriterionStatus.PASSED.value for c in criteria) else "every MUST acceptance criterion passed", **base)
    met = sum(c.status == CriterionStatus.PASSED.value for c in criteria)
    return AcceptanceResult(FAILED, f"{met} of {len(criteria)} acceptance criteria passed; not met: " + "; ".join(f"{c.code} ({c.description})" for c in unmet), **base)


def _evidence(output: str, tests: tuple[str, ...]) -> str:
    """The runner's report for the first failing test, cut short."""
    for test in tests:
        found = re.search(rf"^(?:FAIL|ERROR): {re.escape(test)} .*?(?=^={10,}|^-{10,}\nRan |\Z)", output, re.MULTILINE | re.DOTALL)
        if found:
            return found.group(0).strip()[:_EVIDENCE_CHARS]
    return ""


def for_recovery(result: AcceptanceResult, fingerprint: str) -> VerificationResult:
    """What recovery and the repair brief may see: the unmet criteria by code and description, and nothing of the
    tests. The failing codes stand in for test names, so the same unmet criteria are the same failure."""
    if result.verdict == NOT_RUN:
        return VerificationResult(Verdict.NOT_RUN, f"independent acceptance could not run: {result.reason}", result.exec_status, None, TestCounts(readable=False), (), "",
                                  result.container, result.duration_ms, result.cleanup, fingerprint)  # fmt: skip
    unmet = tuple(c.code for c in result.unmet)
    counts = TestCounts(readable=True, ran=len(result.criteria), failures=len(unmet), failed_names=unmet, summary=f"FAILED (failures={len(unmet)})")
    return VerificationResult(Verdict.FAILED, f"independent acceptance: {result.reason}", result.exec_status, result.exit_code, counts, (), "",
                              result.container, result.duration_ms, result.cleanup, fingerprint)  # fmt: skip
