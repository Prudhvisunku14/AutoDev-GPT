"""Independent browser acceptance for websites: declarative checks, fixed before any code exists, run by DevPilot's verifier.

Why declarative, not test code:
- GPT writes one check per website criterion when it submits the criteria (`submit_criteria`), *before* any code is
  generated. A check is data (go to a page, click a button by its visible name, fill a field by its label, expect a text),
  validated here and executed only by DevPilot's fixed runner (`sandbox_images/verify-web/verify-web.mjs`). No code from a
  model runs in the test harness, so a check cannot be "passed" by code that patches the harness.
- Checks name what a person sees: an accessible role and name, a label, a placeholder, visible text, alt text. A CSS
  selector is allowed but discouraged. So a check states the requirement, not one implementation of it.

Who sees what:
- The checks are frozen on the run (`Run.browser_acceptance_json`, with its SHA-256) the moment the criteria are accepted.
  A copy that no longer matches its hash stops the run.
- The coding provider (OpenCode) never receives them: they are not in the workspace, the brief or any tool result. They are
  mounted read-only into the verification container only, beside a disposable copy of the candidate.
- After a failed run, GPT learns which criteria failed and one plain sentence per failure (the step that failed and why),
  never the full check list, so the repair targets the behaviour, not the checks.

Judgement: a criterion is PASSED only when its own check ran and every step passed with no uncaught page error. A check that
did not run is NOT_RUN, never PASSED. Independent acceptance passes only when every MUST criterion passed and the site's
built-in smoke stages (server, pages, assets, rendering, errors) passed too.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, ValidationError, model_validator

from app.tools.base import ToolArgs

PASSED, FAILED, NOT_RUN = "PASSED", "FAILED", "NOT_RUN"
KIND = "browser"
MAX_STEPS, MAX_CHECKS = 15, 60
ROLES = ("button", "link", "heading", "textbox", "checkbox", "radio", "combobox", "listbox", "option", "img", "navigation", "list", "listitem",
         "form", "region", "main", "banner", "contentinfo", "dialog", "alert", "status", "tab", "tabpanel", "table", "row", "cell", "article", "menu", "menuitem")  # fmt: skip
ACTIONS = ("goto", "click", "fill", "select", "check", "press", "expect_visible", "expect_hidden", "expect_text", "expect_no_text", "expect_url", "expect_title", "expect_count")
TARGETED = {"click", "fill", "select", "check", "expect_visible", "expect_hidden", "expect_count"}
EXPECTATIONS = {"expect_visible", "expect_hidden", "expect_text", "expect_no_text", "expect_url", "expect_title", "expect_count"}
_PATH = re.compile(r"^/[A-Za-z0-9._~\-/#?=&%]{0,199}$")
_KEYS = {"Enter", "Tab", "Escape", "Space", "ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight", "Backspace"}
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=200)]


class BrowserStep(ToolArgs):
    action: Literal["goto", "click", "fill", "select", "check", "press", "expect_visible", "expect_hidden", "expect_text", "expect_no_text", "expect_url", "expect_title", "expect_count"]
    by: Literal["role", "label", "text", "placeholder", "alt", "css"] | None = Field(description="How the target is found: prefer role (with role and target as its accessible name), label, text or placeholder; css only when nothing visible names it")
    role: Literal[ROLES] | None = Field(description="With by=role: the element's ARIA role, e.g. button, link, heading, textbox")  # type: ignore[valid-type]
    target: ShortText | None = Field(description="The accessible name, label, visible text, placeholder, alt text or CSS selector, per `by`")
    value: ShortText | None = Field(description="goto/expect_url: a path such as /about.html; fill/select: the value; press: a key such as Enter; expect_text/expect_no_text/expect_title: the text")
    count: Annotated[int, Field(ge=1, le=100)] | None = Field(description="expect_count: the least number of matching elements")

    @model_validator(mode="before")
    @classmethod
    def _defaults(cls, data: Any) -> Any:
        return {"by": None, "role": None, "target": None, "value": None, "count": None, **data} if isinstance(data, dict) else data


class BrowserCheckSpec(ToolArgs):
    start: Annotated[str, StringConstraints(pattern=r"^/[A-Za-z0-9._~\-/]{0,199}$")] = Field(description="The page the check starts on, e.g. / or /contact.html")
    steps: list[BrowserStep] = Field(min_length=1, max_length=MAX_STEPS)


def step_problems(steps: Iterable[BrowserStep], label: str) -> list[str]:
    problems: list[str] = []
    steps = list(steps)
    for i, s in enumerate(steps, start=1):
        where = f"{label} step {i} ({s.action})"
        if s.action in TARGETED:
            if s.by is None or s.target is None:
                problems.append(f"{where} needs `by` and `target`")
            if s.by == "role" and s.role is None:
                problems.append(f"{where} uses by=role and needs `role`")
            if s.by == "css" and re.search(r"(?i)javascript:|expression\(|url\(", s.target or ""):
                problems.append(f"{where} has a CSS selector that is not plain")
        elif s.by is not None or s.target is not None:
            problems.append(f"{where} takes no `by`/`target`")
        if s.action in ("goto", "expect_url") and (s.value is None or not _PATH.fullmatch(s.value)):
            problems.append(f"{where} needs `value` as a site path starting with /")
        if s.action in ("fill", "select", "expect_text", "expect_no_text", "expect_title") and not s.value:
            problems.append(f"{where} needs `value`")
        if s.action == "press" and s.value not in _KEYS:
            problems.append(f"{where} needs `value` as one of: {', '.join(sorted(_KEYS))}")
        if s.action == "expect_count" and s.count is None:
            problems.append(f"{where} needs `count`")
        if s.action != "expect_count" and s.count is not None:
            problems.append(f"{where} takes no `count`")
    if not any(s.action in EXPECTATIONS for s in steps):
        problems.append(f"{label} must check something: add at least one expect_* step")
    return problems


@dataclass(frozen=True)
class FrozenChecks:
    checks: tuple[dict[str, Any], ...]  # {"code", "priority", "start", "steps": [...]}, in code order
    sha256: str

    def expect(self) -> dict[str, Any]:
        """What the runner reads from /spec/expect.json: the checks and the pages they start on."""
        return {"version": 2, "routes": sorted({c["start"] for c in self.checks}), "checks": [{"id": c["code"], "start": c["start"], "steps": c["steps"]} for c in self.checks]}


def _canonical(checks: Iterable[Mapping[str, Any]]) -> str:
    return json.dumps(list(checks), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def freeze(checks: Iterable[Mapping[str, Any]]) -> tuple[str, str]:
    data = _canonical(sorted(checks, key=lambda c: c["code"]))
    if len(json.loads(data)) > MAX_CHECKS:
        raise ValueError(f"at most {MAX_CHECKS} browser checks")
    return data, hashlib.sha256(data.encode("utf-8")).hexdigest()


class ChecksChanged(ValueError):
    pass


def thaw(data: str, sha256: str) -> FrozenChecks:
    if hashlib.sha256(data.encode("utf-8")).hexdigest() != sha256:
        raise ChecksChanged("the frozen browser acceptance checks no longer match their hash, so the run stops")
    try:
        checks = json.loads(data)
        for c in checks:
            spec = BrowserCheckSpec.model_validate({"start": c["start"], "steps": c["steps"]})
            problems = step_problems(spec.steps, c["code"])
            if problems:
                raise ChecksChanged("; ".join(problems))
    except (ValueError, KeyError, TypeError, ValidationError) as exc:
        raise ChecksChanged(f"the frozen browser acceptance checks cannot be read: {exc}") from None
    return FrozenChecks(tuple(checks), sha256)


def describe_step(step: Mapping[str, Any]) -> str:
    """One step in words, for the person and for GPT's repair: never more than the step itself says."""
    action, by, role, target, value = (step.get(k) for k in ("action", "by", "role", "target", "value"))
    what = f"the {role} named {target!r}" if by == "role" else f"the element with {by} {target!r}" if by else ""
    return {
        "goto": f"open {value}", "click": f"click {what}", "fill": f"type {value!r} into {what}", "select": f"choose {value!r} in {what}", "check": f"tick {what}",
        "press": f"press {value}", "expect_visible": f"see {what}", "expect_hidden": f"not see {what}", "expect_text": f"see the text {value!r}",
        "expect_no_text": f"not see the text {value!r}", "expect_url": f"be on {value}", "expect_title": f"see the page title {value!r}",
        "expect_count": f"see at least {step.get('count')} of {what}",
    }.get(str(action), str(action))  # fmt: skip


@dataclass(frozen=True)
class CriterionOutcome:
    code: str
    priority: str
    status: str
    detail: str  # one plain sentence: what failed and why, or "passed"


def judge(checks: FrozenChecks, results: Iterable[Mapping[str, Any]] | None, *, smoke_passed: bool, smoke_reason: str) -> dict[str, Any]:
    """The run's acceptance record (stored as Run.acceptance_json and shown in the report)."""
    by_id = {str(r.get("id")): r for r in results or [] if isinstance(r, Mapping)}
    outcomes: list[CriterionOutcome] = []
    for c in checks.checks:
        r = by_id.get(c["code"])
        if r is None:
            outcomes.append(CriterionOutcome(c["code"], c["priority"], NOT_RUN, "the check did not run"))
        elif r.get("ok") is True and not r.get("errors"):
            outcomes.append(CriterionOutcome(c["code"], c["priority"], PASSED, "passed"))
        else:
            index = r.get("failed_step")
            step = c["steps"][index] if isinstance(index, int) and 0 <= index < len(c["steps"]) else None
            reason = str(r.get("detail") or "failed")[:300]
            errors = [str(e)[:200] for e in r.get("errors") or []][:3]
            sentence = (f"step {index + 1}: expected to {describe_step(step)}: {reason}" if step else reason) + (f"; uncaught page error: {'; '.join(errors)}" if errors else "")
            outcomes.append(CriterionOutcome(c["code"], c["priority"], FAILED, sentence))
    must = [o for o in outcomes if o.priority == "MUST"]
    unmet = [o for o in must if o.status != PASSED]
    if not results and not smoke_passed:
        verdict, reason = NOT_RUN, f"the browser checks did not run: {smoke_reason}"
    elif unmet:
        verdict = FAILED if any(o.status == FAILED for o in unmet) else NOT_RUN
        reason = "unmet MUST criteria: " + ", ".join(o.code for o in unmet)
    elif not smoke_passed:
        verdict, reason = FAILED, f"every MUST criterion passed, but the site's own checks failed: {smoke_reason}"
    else:
        verdict, reason = PASSED, "every MUST criterion passed in a real browser, and the site's built-in checks passed"
    return {
        "configured": True, "kind": KIND, "verdict": verdict, "reason": reason, "suite_sha256": checks.sha256,
        "criteria": [{"code": o.code, "priority": o.priority, "status": o.status, "detail": o.detail, "description": o.detail} for o in outcomes],
        "ran": sum(o.status != NOT_RUN for o in outcomes), "passed": sum(o.status == PASSED for o in outcomes), "total": len(outcomes),
    }


def for_gpt(judged: Mapping[str, Any], descriptions: Mapping[str, str]) -> dict[str, Any]:
    """What GPT is told after a run: the verdict and, per criterion that did not pass, its text and one sentence. Never the checks."""
    return {
        "kind": KIND, "verdict": judged.get("verdict"), "reason": judged.get("reason"),
        "unmet": [{"code": c["code"], "priority": c["priority"], "criterion": descriptions.get(c["code"], ""), "what_happened": c["detail"]}
                  for c in judged.get("criteria", []) if c.get("status") != PASSED],
    }  # fmt: skip
