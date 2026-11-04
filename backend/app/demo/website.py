"""A static website built through the real pipeline, with GPT scripted and the coding provider simulated.

It exists to prove the engine's website path end to end without spending money: analysis, a plan on `static-html`, criteria
whose browser checks are frozen before any code exists, a first attempt that breaks one criterion, verification by the
website verifier (the real Docker one in the opt-in integration test), a repair through the normal bounded recovery, a
second verification that passes independent acceptance, delivery and the final report. The site is generic: nothing here is
tied to any customer, repository or brand.

GPT and the coding provider are scripted, so a run of this scenario is never presented as real generation: the report's
provenance lists both as simulated, and the delivery gate refuses it a ZIP for that reason.
"""

from __future__ import annotations

from typing import Any

from app.coding.simulated import ScriptedAttempt
from app.orchestrator.fake_llm import Step, call
from app.verification import web_fixtures

REQUIREMENT = (
    "Build a small studio homepage as a static website: a menu button that reveals the main navigation, a work section listing "
    "projects from a data list, and a contact form that refuses an invalid email or empty message and confirms a valid one."
)
_HTML = {"language": "html", "framework": None, "multi_file": True, "size": "small"}

ANALYSIS: dict[str, Any] = {
    "summary": "A one-page static studio site with a toggled navigation menu, a data-driven project list and a validated contact form.",
    "requirements": [
        {"code": "R-001", "text": "A menu button reveals the main navigation, which links to the page's sections.", "kind": "functional"},
        {"code": "R-002", "text": "The work section lists every project from a data list.", "kind": "functional"},
        {"code": "R-003", "text": "The contact form refuses an invalid email or an empty message and confirms a valid submission.", "kind": "functional"},
        {"code": "R-004", "text": "The page has a descriptive title.", "kind": "non_functional"},
    ],
    "assumptions": ["Form submission is a local demonstration: nothing is sent anywhere."],
    "ambiguities": [],
}
PLAN: dict[str, Any] = {
    "stack": "static-html",
    "stack_rationale": "A single page with light interactivity needs no framework or build step.",
    "summary": "index.html with semantic sections, style.css, and app.js for the menu, the project list and form validation.",
    "tasks": [{"title": "Studio homepage", "description": "index.html, style.css and app.js implementing every requirement.", "capability": "code_generation",
               "requirement_refs": ["R-001", "R-002", "R-003", "R-004"], "features": _HTML}],
}  # fmt: skip

_TEXT = {
    "AC-001": ("the homepage", "the visitor opens the menu and follows the Contact link", "the navigation appears and the page moves to the contact section", ["R-001"]),
    "AC-002": ("the homepage", "the work section is shown", "every project in the data list appears", ["R-002"]),
    "AC-003": ("the contact form", "it is sent empty, then with a valid email and message", "it first asks for valid input, then confirms", ["R-003"]),
    "AC-004": ("the homepage", "it loads", "its title names the studio", ["R-004"]),
}


def _criterion(check: dict[str, Any]) -> dict[str, Any]:
    given, when, then, refs = _TEXT[str(check["code"])]
    return {"code": check["code"], "given": given, "when": when, "then": then, "priority": check["priority"], "method": "e2e", "requirement_refs": refs,
            "browser": {"start": check["start"], "steps": check["steps"]}}  # fmt: skip


CRITERIA: dict[str, Any] = {"criteria": [_criterion(dict(c)) for c in web_fixtures.ACCEPTANCE_CHECKS]}

FIRST_ATTEMPT = ScriptedAttempt(dict(web_fixtures._INTERACTIVE_BROKEN))  # every file, but the form accepts anything
REPAIR_ATTEMPT = ScriptedAttempt({"app.js": web_fixtures._INTERACTIVE["app.js"]})  # the validation restored


def gpt_script() -> list[Step]:
    return [
        call("submit_analysis", ANALYSIS), call("submit_plan", PLAN), call("submit_criteria", CRITERIA),
        call("generate_code", {"task_order": 1, "instructions": "Build the homepage."}), call("submit_implementation", {"summary": "The homepage is done."}),
        call("generate_code", {"task_order": 1, "instructions": "Fix the contact form validation the acceptance run reported."}),
        call("submit_implementation", {"summary": "Validation fixed."}), call("finish", {"summary": "The homepage passes independent browser acceptance."}),
    ]  # fmt: skip
