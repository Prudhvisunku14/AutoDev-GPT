from __future__ import annotations

import pytest

from app.evaluation.gaps import compare_document_versions, compare_requirements
from app.evaluation.tool_explain import compare_tools, tool_catalog


def test_gap_analysis_is_conservative_even_with_matching_keywords() -> None:
    source = {"src/booking.py": b"def book_appointment(slot):\n    return slot\n", "README.md": b"Appointment scheduling prototype"}
    result = compare_requirements([("R1", "Prevent double appointment bookings"), ("R2", "Export monthly invoices")], source, source.__getitem__)
    assert result["verified_requirements"] == 0
    assert result["requirements"][0]["status"] == "unverified_candidate"
    assert result["requirements"][1]["status"] == "no_source_evidence_found"


def test_gap_analysis_refuses_unsafe_names_and_duplicate_requirement_ids() -> None:
    source = {".env": b"SECRET=secret", "app.py": b"export bookings"}
    result = compare_requirements([("T1", "secret credentials")], source, source.__getitem__)
    assert result["files_inspected"] == 1
    assert not result["requirements"][0]["candidate_files"]
    with pytest.raises(ValueError):
        compare_requirements([("X", "first"), ("X", "second")], source, source.__getitem__)


def test_document_change_reports_exact_text_not_policy_interpretation() -> None:
    result = compare_document_versions("Expense deadline 7 days", "Expense deadline 10 days", "v1", "v2")
    assert result["changed"]
    assert result["exact_changes"][0]["before"] == "Expense deadline 7 days"
    assert result["exact_changes"][0]["after"] == "Expense deadline 10 days"
    assert result["interpretation"] == "not performed"


def test_document_change_no_change() -> None:
    assert not compare_document_versions("a", "a", "v1", "v2")["changed"]


class FakeRisk:
    value = "READ"


class FakeSpec:
    risk = FakeRisk()
    states = None
    external = None
    description = "Registered adapter, not proof of actual connectivity"


class FakeRegistry:
    names = ["first", "second"]

    def get(self, name: str):
        return FakeSpec() if name in self.names else None


def test_tool_catalog_does_not_claim_verification() -> None:
    value = tool_catalog(FakeRegistry())
    assert value["total"] == 2
    assert "not established" in value["tools"][0]["verified_connectivity"]
    assert len(compare_tools(FakeRegistry(), "first", "second")["tools"]) == 2
    with pytest.raises(ValueError):
        compare_tools(FakeRegistry(), "first", "missing")
