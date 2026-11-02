"""Repository evaluation on fixtures (invented repositories, never GitHub data): licences, evidence, untrusted text, decisions."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from app.repos import evaluate as judge
from app.repos.fixtures import GONE, OWNER, FixtureSource
from app.repos.licenses import judge_license
from app.repos.model import FIXTURE
from app.repos.source import RepositoryUnavailable

LIMITS = judge.Limits(max_size_kb=80_000, max_files=2_000)
TODAY = date(2026, 11, 2)
QUERY = "weather dashboard react fastapi city search"


def evaluate(name: str, must_have: tuple[str, ...] = ("react", "fastapi"), query: str = QUERY) -> judge.Evaluation:
    return judge.evaluate(FixtureSource().detail(f"{OWNER}/{name}"), query=query, must_have=must_have, limits=LIMITS, today=TODAY)


@pytest.mark.parametrize(("spdx", "allowed"), [("MIT", True), ("Apache-2.0", True), ("BSD-3-Clause", True), ("GPL-3.0", False), ("AGPL-3.0", False), ("LGPL-2.1", False), (None, False), ("NOASSERTION", False), ("WTFPL", False)])
def test_only_permissive_licences_are_reused(spdx: str | None, allowed: bool) -> None:
    assert judge_license(spdx, "licence text").allowed is allowed


def test_a_permissive_licence_without_its_file_is_refused_because_attribution_could_not_be_kept() -> None:
    verdict = judge_license("MIT", "")
    assert not verdict.allowed and "attribution" in verdict.reason


def test_the_suitable_repository_is_eligible_and_shows_each_technology_in_its_own_files() -> None:
    e = evaluate("react-fastapi-weather")
    assert e.eligible and e.evidence == {"react": True, "fastapi": True} and e.tests_present and e.license and e.license.spdx == "MIT" and e.source == FIXTURE


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("django-weather-portal", "not shown by the repository's own files: react, fastapi"),
        ("weather-dashboard-gpl", "copyleft"),
        ("weather-dashboard-unlicensed", "no licence"),
        ("fastapi-react-weather-plus", "untrusted instructions"),
    ],
)
def test_unsuitable_repositories_are_refused_with_their_reason(name: str, reason: str) -> None:
    e = evaluate(name)
    assert not e.eligible and any(reason in r for r in e.reasons)


def test_instructions_aimed_at_agents_and_install_hooks_are_found_and_never_followed() -> None:
    findings = judge.scan_untrusted(FixtureSource().detail(f"{OWNER}/fastapi-react-weather-plus"))
    kinds = {(f.severity, f.where) for f in findings}
    assert (judge.HIGH, "README") in kinds and any(f.severity == judge.MEDIUM and "postinstall" in f.what for f in findings)


def test_gpt_gets_the_evaluation_and_never_the_readme() -> None:
    shown = evaluate("fastapi-react-weather-plus").for_gpt()
    text = repr(shown)
    assert "ignore all previous instructions" not in text and "curl" not in text and len(shown["untrusted_description"]) <= 200


def test_a_gone_repository_is_recorded_as_unavailable() -> None:
    source = FixtureSource()
    summary = next(s for s in source.search("weather", limit=10) if s.full_name == GONE)
    with pytest.raises(RepositoryUnavailable):
        source.detail(GONE)
    e = judge.unavailable(summary, source.name, "gone")
    assert not e.available and not e.eligible


def test_reuse_is_decided_only_for_an_eligible_repository_good_enough_to_start_from() -> None:
    good, gpl = evaluate("react-fastapi-weather"), evaluate("weather-dashboard-gpl")
    decision = judge.decide([gpl, good], threshold=0.55)
    assert decision.action == "reuse" and decision.full_name == good.full_name and decision.commit == good.commit and decision.license == "MIT"
    assert judge.decide([good], threshold=0.99).action == "build" and judge.decide([gpl], threshold=0.0).action == "build" and judge.decide([], threshold=0.0).action == "build"


def test_the_rules_are_not_about_weather() -> None:
    portal = evaluate("django-weather-portal", must_have=("django",), query="django portal templates")
    assert portal.eligible and portal.evidence == {"django": True}


def test_an_archived_or_oversized_repository_is_refused() -> None:
    detail = FixtureSource().detail(f"{OWNER}/react-fastapi-weather")
    archived = judge.evaluate(replace(detail, summary=replace(detail.summary, archived=True)), query=QUERY, must_have=("react",), limits=LIMITS, today=TODAY)
    small = judge.evaluate(detail, query=QUERY, must_have=("react",), limits=judge.Limits(max_size_kb=0, max_files=1), today=TODAY)
    assert not archived.eligible and not small.eligible and any("too large" in r for r in small.reasons)


def test_nothing_matches_a_search_for_something_else() -> None:
    assert FixtureSource().search("quantum spreadsheet compiler", limit=10) == []
