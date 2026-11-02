"""The offline competition demonstration: every scenario ends as expected, through the real API, and says what was
simulated. No model, key, Docker container or network is involved."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.demo import competition

KEYS = ["demo1_success", "demo2_repair", "demo3_security", "demo4_alternative", "demo4_refusal", "acceptance_check"]


@pytest.fixture(scope="module")
def demo(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, dict[str, Any]]]:
    out = tmp_path_factory.mktemp("competition")
    results, _settings = competition.run_all(out)
    competition.write_reports(out, results)
    return out, {r["key"]: r for r in results}


def test_every_scenario_ends_as_expected(demo: tuple[Path, dict[str, dict[str, Any]]]) -> None:
    _out, by = demo
    assert list(by) == KEYS
    assert all(r["as_expected"] for r in by.values()), {k: r["checks"] for k, r in by.items() if not r["as_expected"]}


def test_each_demo_shows_what_it_claims(demo: tuple[Path, dict[str, dict[str, Any]]]) -> None:
    _out, by = demo
    assert by["demo1_success"]["actual"]["delivered"] and by["demo1_success"]["report"]["acceptance"]["verdict"] == "PASSED"
    assert [d["category"] for d in by["demo2_repair"]["report"]["recovery"]] == ["TEST_FAILURE"] and by["demo2_repair"]["report"]["verifications"][0]["failed_names"] == ["test_ac_002_subtraction"]
    security = by["demo3_security"]["report"]
    assert security["coding_attempts"][0]["violations"] and security["recovery"][0]["action"] == "STOP" and not security["delivered_files"]
    assert by["demo4_alternative"]["actual"]["providers"] == ["simulated_backup"]
    refusal = by["demo4_refusal"]["report"]
    assert refusal["coding_attempts"] == [] and refusal["recovery"][0]["failure_class"] == "NO_ELIGIBLE_TOOL"
    assert by["acceptance_check"]["actual"]["acceptance"] == ["FAILED", "PASSED"]


def test_opencode_is_refused_by_policy_and_never_started(demo: tuple[Path, dict[str, dict[str, Any]]]) -> None:
    _out, by = demo
    for result in by.values():
        for choice in result["report"]["selection"]:
            opencode = next(r for r in choice["rejected"] if r["tool"] == "opencode_server")
            assert "CREDENTIALS" in opencode["codes"] and "NETWORK" in opencode["codes"]
        assert "opencode_server" not in {a["provider"] for a in result["report"]["coding_attempts"]}


def test_every_report_names_what_was_simulated(demo: tuple[Path, dict[str, dict[str, Any]]]) -> None:
    _out, by = demo
    for result in by.values():
        provenance = result["report"]["provenance"]
        assert provenance["fully_real"] is False and "GPT orchestration" in provenance["simulated"] and provenance["gpt"] == "FakeLLM (scripted)"
        assert "test execution (replayed from recorded runs, not Docker)" in provenance["simulated"] and provenance["sandbox"] == "ReplaySandbox"


def test_the_reports_are_written(demo: tuple[Path, dict[str, dict[str, Any]]]) -> None:
    out, by = demo
    summary = json.loads((out / "competition-demo.json").read_text(encoding="utf-8"))
    assert [s["key"] for s in summary["scenarios"]] == KEYS and "test execution (ReplaySandbox, not Docker)" in summary["simulated"]
    for key in KEYS:
        assert json.loads((out / "reports" / f"{key}.json").read_text(encoding="utf-8"))["key"] == key
        assert "Independent acceptance" in (out / "reports" / f"{key}.md").read_text(encoding="utf-8")


def test_a_folder_the_demonstration_did_not_make_is_left_alone(tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_text("mine", encoding="utf-8")
    with pytest.raises(SystemExit, match="not made by this demonstration"):
        competition._prepare(tmp_path)
    assert (tmp_path / "keep.txt").read_text(encoding="utf-8") == "mine"


def test_its_own_earlier_folder_is_cleared(tmp_path: Path) -> None:
    competition._prepare(tmp_path)
    (tmp_path / "old.json").write_text("{}", encoding="utf-8")
    competition._prepare(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == [competition.MARKER]
