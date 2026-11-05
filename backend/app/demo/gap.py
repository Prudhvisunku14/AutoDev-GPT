"""A scripted gap analysis for the offline demonstrations that start from imported code (tools/adapters/gap.py).

It is what a scripted GPT submits after an import: every functional requirement of the scripted analysis is listed as a
feature to build on the reused code, with a test for each, and one new test file. It must still pass the same checks a
real analysis does.
"""

from __future__ import annotations

from typing import Any

from app.orchestrator.fake_llm import Step, call


def gap_analysis(analysis: dict[str, Any], *, test_path: str, component: str = "the imported repository") -> dict[str, Any]:
    functional = [r for r in analysis["requirements"] if r["kind"] == "functional"]
    return {
        "existing_functionality": [f"{component} provides working code to start from"],
        "similarities": [],
        "missing_features": [{"requirement_ref": r["code"], "description": r["text"][:500]} for r in functional],
        "conflicts": [],
        "architecture_changes": [f"adapt {component} instead of writing it again"],
        "files": [{"path": test_path, "change": "add", "reason": "tests that verify every requirement on the reused code"}],
        "tests": [{"requirement_ref": r["code"], "description": f"a test for {r['code']}"} for r in functional],
        "reuse_decisions": [{"component": component, "decision": "adapt", "reason": "it already does part of the job and its licence allows reuse"}],
    }


def gap_step(analysis: dict[str, Any], *, test_path: str, component: str = "the imported repository") -> Step:
    return call("submit_gap_analysis", gap_analysis(analysis, test_path=test_path, component=component))
