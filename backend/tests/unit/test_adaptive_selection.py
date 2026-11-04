"""The adaptive selection policy, decided on its own: no database, no Docker, no model, no network."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

from app.core.egress import EgressPolicy
from app.selection.adaptive import AdaptiveSelector, EligibilityPolicy, ScoringConfig, Weights, available_memory_mb, efficiency, expected, reliability
from app.selection.catalog import CODING_CAPABILITIES, OPENCODE_NAME, PLANNED_TOOLS, SIMULATED_PROFILE, build_selector, eligibility_policy, opencode_profile
from app.selection.model import BUILD, MODIFY, REAL, REPAIR, SIMULATED, Refusal, SelectionContext, ToolHistory, ToolProfile, ToolStatus, task_category
from app.selection.selector import NoProvider, ProviderOption, ProviderSelector, Selection
from tests.helpers import ALLOWLIST_FILE, make_settings

CAPABILITY = "code_generation"
STACK = "python-cli"
POLICY = EligibilityPolicy(approved_environments=frozenset({"docker-internal", "in-process"}), configured_credentials=frozenset({"OPENAI_API_KEY"}), approved_network=frozenset({"api.openai.com"}))
KNOWN = {"quality": 0.8, "seconds": 120.0, "cost_usd": 0.25, "min_cost_usd": 0.1, "memory_mb": 2048}


class Launcher:
    def __init__(self, reason: str | None = None) -> None:
        self.reason = reason
        self.asked = 0

    def unavailable_reason(self) -> str | None:
        self.asked += 1
        return self.reason


def profile(name: str = "tool", **changes: Any) -> ToolProfile:
    values: dict[str, Any] = {"status": ToolStatus.IMPLEMENTED, "capabilities": {CAPABILITY: 1.0}, "stacks": {STACK: 1.0}, "kinds": {BUILD: 1.0, REPAIR: 1.0}, "environment": "docker-internal", **KNOWN, **changes}
    return ToolProfile(name, **values)


def option(name: str = "tool", *, simulated: bool = False, reason: str | None = None, **changes: Any) -> ProviderOption:
    return ProviderOption(name, Launcher(reason), frozenset(changes.get("capabilities", {CAPABILITY: 1.0})), simulated=simulated, profile=profile(name, **changes))  # type: ignore[arg-type]


def selector(*options: ProviderOption, policy: EligibilityPolicy = POLICY, memory: int | None = None, **kwargs: Any) -> AdaptiveSelector:
    return AdaptiveSelector(list(options), policy=policy, memory_probe=lambda: memory, **kwargs)


def context(**changes: Any) -> SelectionContext:
    return SelectionContext(**{"stack": STACK, **changes})


def history(tool: str, successes: int, failures: int = 0, *, evidence: str = REAL, **changes: Any) -> dict[tuple[str, str], ToolHistory]:
    return {(tool, evidence): ToolHistory(attempts=successes + failures, successes=successes, failures={"TEST_FAILURE": failures} if failures else {}, **changes)}


def choose(chooser: AdaptiveSelector, **changes: Any) -> Selection:
    return chooser.choose(CAPABILITY, context(**changes))


def codes(chooser: AdaptiveSelector, tool: str, capability: str = CAPABILITY, **changes: Any) -> set[Refusal]:
    """What refused `tool`, with a harmless second tool present so a choice can still be made."""
    try:
        selection = chooser.choose(capability, context(**changes))
    except NoProvider as exc:
        assert tool in exc.excluded
        rejected = chooser._options  # nothing was chosen, so ask the checks directly
        from app.selection.adaptive import refusals

        target = next(o for o in rejected if o.name == tool)
        return {code for code, _ in refusals(target, capability, context(**changes), chooser._policy, chooser._config, chooser._memory_probe())}
    assert selection.report is not None
    return {code for r in selection.report.rejected if r.tool_id == tool for code in r.codes}


# --- Capability matching ---------------------------------------------------------------------------------


def test_a_tool_that_cannot_do_the_task_is_refused() -> None:
    chooser = selector(option("docs", capabilities={"documentation": 1.0}), option("code"))
    assert choose(chooser).provider == "code" and Refusal.CAPABILITY in codes(chooser, "docs")


def test_a_stack_with_no_verified_path_is_refused_rather_than_claimed() -> None:
    chooser = selector(option("python"))
    with pytest.raises(NoProvider, match="no verified path for the react-vite stack"):
        chooser.choose(CAPABILITY, context(stack="react-vite"))


def test_a_task_kind_the_tool_does_not_do_is_refused() -> None:
    with pytest.raises(NoProvider, match="does not do modify tasks"):
        selector(option("builder")).choose(CAPABILITY, context(kind=MODIFY))


def test_the_capability_score_is_the_mean_of_what_the_task_needs() -> None:
    chooser = selector(option("t", capabilities={CAPABILITY: 0.9}, stacks={STACK: 0.6}, kinds={BUILD: 0.3}))
    report = choose(chooser).report
    assert report is not None and report.ranking[0].capability == pytest.approx(0.6)


def test_a_declared_suitability_outside_the_unit_range_is_refused() -> None:
    with pytest.raises(ValueError, match="between 0 and 1"):
        profile(capabilities={CAPABILITY: 1.5})


def test_task_categories_keep_different_work_apart() -> None:
    assert task_category(CAPABILITY, STACK, BUILD) != task_category(CAPABILITY, STACK, REPAIR) != task_category("documentation", STACK, REPAIR)
    assert task_category(CAPABILITY, STACK, BUILD) != task_category(CAPABILITY, "react-vite", BUILD)


# --- Eligibility: mandatory, and no score can overcome it --------------------------------------------------

STRONG = {"quality": 1.0, "seconds": 0.0, "cost_usd": 0.0, "min_cost_usd": 0.0, "memory_mb": 1}
REFUSED = [
    ("capability", {"capabilities": {"documentation": 1.0}}, {}, Refusal.CAPABILITY),
    ("stack", {"stacks": {"react-vite": 1.0}}, {}, Refusal.STACK),
    ("task kind", {"kinds": {REPAIR: 1.0}}, {}, Refusal.TASK_KIND),
    ("not implemented", {"status": ToolStatus.PLANNED}, {}, Refusal.NOT_IMPLEMENTED),
    ("unavailable", {}, {"reason": "docker is down"}, Refusal.UNAVAILABLE),
    ("credential", {"credentials": frozenset({"GITHUB_TOKEN"})}, {}, Refusal.CREDENTIALS),
    ("network", {"network": frozenset({"evil.example"})}, {}, Refusal.NETWORK),
    ("host execution", {"isolated": False}, {}, Refusal.SECURITY),
    ("writes the real project", {"disposable_copy_only": False}, {}, Refusal.SECURITY),
    ("environment", {"environment": "host-shell"}, {}, Refusal.ENVIRONMENT),
    ("budget", {"min_cost_usd": 5.0}, {}, Refusal.BUDGET),
    ("memory", {"memory_mb": 9_999}, {}, Refusal.RESOURCES),
]


@pytest.mark.parametrize(("changes", "extra", "code"), [r[1:] for r in REFUSED], ids=[r[0] for r in REFUSED])
def test_a_refused_tool_cannot_win_however_well_it_would_score(changes: dict[str, Any], extra: dict[str, Any], code: Refusal) -> None:
    perfect = option("perfect", **{**STRONG, **changes}, **extra)
    weak = option("weak", quality=0.0, seconds=9_999.0, cost_usd=0.01, min_cost_usd=0.01, memory_mb=100)
    chooser = selector(perfect, weak, memory=1_000)
    selection = choose(chooser, remaining_usd=1.0, history=history("perfect", 500))
    assert selection.provider == "weak" and code in codes(chooser, "perfect", remaining_usd=1.0, history=history("perfect", 500))


def test_every_reason_is_reported_not_only_the_first() -> None:
    chooser = selector(option("bad", isolated=False, environment="host-shell", credentials=frozenset({"X"})))
    with pytest.raises(NoProvider) as caught:
        choose(chooser)
    reason = caught.value.excluded["bad"]
    assert "host" in reason and "environment" in reason and "credential" in reason


def test_a_launcher_is_not_asked_about_a_tool_already_refused() -> None:
    refused = option("bad", isolated=False)
    with pytest.raises(NoProvider):
        choose(selector(refused))
    assert refused.launcher.asked == 0  # type: ignore[attr-defined]


def test_with_nothing_eligible_the_choice_stops_and_says_why() -> None:
    with pytest.raises(NoProvider, match="a: docker is down; b: it would execute on the host"):
        choose(selector(option("a", reason="docker is down"), option("b", isolated=False)))


def test_with_no_tool_at_all_it_says_so() -> None:
    with pytest.raises(NoProvider, match="none is configured"):
        choose(selector())


def test_planned_tools_are_reported_and_never_chosen() -> None:
    chooser = selector(option("real"), planned=PLANNED_TOOLS)
    selection = choose(chooser)
    assert selection.provider == "real" and {"opencode_via_n8n", "gpt_writer"} <= set(selection.excluded)
    assert all(p.status is ToolStatus.PLANNED for p in PLANNED_TOOLS)


def test_only_planned_tools_means_no_tool() -> None:
    with pytest.raises(NoProvider, match="planned and has no implementation"):
        choose(selector(planned=PLANNED_TOOLS))


# --- Scores ------------------------------------------------------------------------------------------------


def test_the_initial_weights_are_the_agreed_ones() -> None:
    weights = Weights()
    assert (weights.capability, weights.reliability, weights.quality, weights.latency, weights.cost, weights.resource) == (0.30, 0.25, 0.15, 0.10, 0.10, 0.10)


def test_the_score_is_the_weighted_sum_worked_by_hand() -> None:
    report = choose(selector(option("t")), history=history("t", 3, 1, seconds_total=480.0, seconds_samples=4, cost_total=1.0, cost_samples=4)).report
    assert report is not None
    best = report.ranking[0]
    assert best.reliability == pytest.approx(5 / 8) and best.latency == pytest.approx(0.5) and best.cost == pytest.approx(0.5) and best.resource == pytest.approx(0.5)
    assert best.score == pytest.approx(0.30 * 1.0 + 0.25 * 5 / 8 + 0.15 * 0.8 + 0.10 * 0.5 + 0.10 * 0.5 + 0.10 * 0.5)


@pytest.mark.parametrize(("seconds", "cost", "memory", "quality"), [(0.0, 0.0, 1, 0.0), (1e9, 1e9, 10**9, 1.0), (None, None, None, None)], ids=["zero", "enormous", "unknown"])
def test_every_metric_stays_between_0_and_1(seconds: float | None, cost: float | None, memory: int | None, quality: float | None) -> None:
    report = choose(selector(option("t", seconds=seconds, cost_usd=cost, memory_mb=memory, quality=quality, min_cost_usd=0.0))).report
    assert report is not None
    best = report.ranking[0]
    assert all(0.0 <= value <= 1.0 for value in (best.score, best.capability, best.reliability, best.quality, best.latency, best.cost, best.resource))


def test_efficiency_cannot_divide_by_zero() -> None:
    assert efficiency(0.0, 120.0, 0.25) == 1.0 and efficiency(120.0, 120.0, 0.25) == 0.5 and efficiency(None, 120.0, 0.25) == 0.25
    assert efficiency(-5.0, 120.0, 0.25) == 1.0  # a nonsense reading cannot score above a perfect one


def test_a_score_does_not_change_when_an_unrelated_tool_is_added() -> None:
    alone = choose(selector(option("a", cost_usd=0.2))).report
    together = choose(selector(option("a", cost_usd=0.2), option("b", cost_usd=99.0, seconds=9_000.0))).report
    assert alone is not None and together is not None
    assert next(c for c in together.ranking if c.tool_id == "a").score == alone.ranking[0].score


def test_weights_are_configurable_and_change_the_choice() -> None:
    fast, cheap = option("fast", seconds=1.0, cost_usd=2.0), option("cheap", seconds=900.0, cost_usd=0.01)
    assert choose(selector(fast, cheap, config=ScoringConfig(weights=Weights(0.0, 0.0, 0.0, 1.0, 0.0, 0.0)))).provider == "fast"
    assert choose(selector(fast, cheap, config=ScoringConfig(weights=Weights(0.0, 0.0, 0.0, 0.0, 1.0, 0.0)))).provider == "cheap"


@pytest.mark.parametrize("values", [(0.5, 0.5, 0.5, 0.0, 0.0, 0.0), (0.1, 0.1, 0.1, 0.1, 0.1, 0.1), (1.2, -0.2, 0.0, 0.0, 0.0, 0.0)], ids=["over", "under", "negative"])
def test_weights_that_do_not_add_up_are_refused(values: tuple[float, ...]) -> None:
    with pytest.raises(ValueError, match="selection weights"):
        Weights(*values)


def test_the_settings_refuse_weights_that_do_not_add_up(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="add up to 1"):
        make_settings(tmp_path, selection_weight_capability=0.9)


def test_the_settings_reach_the_scoring(tmp_path: Path) -> None:
    config = ScoringConfig.from_settings(make_settings(tmp_path, selection_reliability_alpha=3.0, selection_weight_quality=0.05, selection_weight_cost=0.20, selection_cost_reference_usd=1.0))
    assert config.alpha == 3.0 and config.weights.quality == 0.05 and config.weights.cost == 0.20 and config.cost_reference_usd == 1.0


# --- Reliability -------------------------------------------------------------------------------------------


def test_reliability_is_the_agreed_formula() -> None:
    assert reliability(7, 10, 2.0, 2.0) == pytest.approx(9 / 14)


def test_an_untested_tool_is_never_treated_as_perfectly_reliable() -> None:
    assert reliability(0, 0, 2.0, 2.0) == 0.5


def test_one_result_cannot_distort_the_estimate() -> None:
    assert reliability(1, 1, 2.0, 2.0) == pytest.approx(0.6) and reliability(0, 1, 2.0, 2.0) == pytest.approx(0.4)


def test_reliability_approaches_but_never_reaches_certainty() -> None:
    assert 0.99 < reliability(1_000, 1_000, 2.0, 2.0) < 1.0 and 0.0 < reliability(0, 1_000, 2.0, 2.0) < 0.01


def test_reliability_moves_the_right_way_with_more_evidence() -> None:
    values = [reliability(n, n, 2.0, 2.0) for n in range(20)]
    assert values == sorted(values) and len(set(values)) == 20


@pytest.mark.parametrize(("alpha", "beta"), [(0.0, 2.0), (2.0, 0.0), (-1.0, 2.0)])
def test_a_prior_that_could_give_0_or_1_is_refused(alpha: float, beta: float) -> None:
    with pytest.raises(ValueError, match="alpha and beta"):
        ScoringConfig(alpha=alpha, beta=beta)


def test_failures_lower_a_tool_below_an_untested_one() -> None:
    selection = choose(selector(option("failing"), option("untested")), history=history("failing", 0, 3))
    assert selection.provider == "untested"


def test_a_proven_tool_beats_an_untested_one() -> None:
    assert choose(selector(option("untested"), option("proven")), history=history("proven", 5)).provider == "proven"


def test_with_evidence_on_both_the_more_reliable_tool_wins_though_it_costs_more() -> None:
    """Why the benchmark's control scenario loses: the weights are sound, but nothing makes the selector try the untested tool."""
    dear = {"quality": None, "seconds": 60.0, "cost_usd": 0.20, "min_cost_usd": 0.20, "memory_mb": 1_024}
    cheap = {"quality": None, "seconds": 30.0, "cost_usd": 0.10, "min_cost_usd": 0.10, "memory_mb": 800}
    chooser = selector(option("reliable", **dear), option("cheap", **cheap))
    assert choose(chooser, history=history("cheap", 21, 9)).provider == "cheap"  # the better tool is untested, so it is never tried
    assert choose(chooser, history=history("cheap", 21, 9) | history("reliable", 27, 3)).provider == "reliable"


# --- Missing data ------------------------------------------------------------------------------------------


def test_missing_history_is_reported_not_hidden() -> None:
    report = choose(selector(option("t"))).report
    assert report is not None and report.confidence == 0.0 and "reliability" in report.ranking[0].unknown and "only the prior" in report.explanation


def test_confidence_grows_with_verified_evidence() -> None:
    few = choose(selector(option("t")), history=history("t", 2)).report
    many = choose(selector(option("t")), history=history("t", 50)).report
    assert few is not None and many is not None and 0 < few.confidence < many.confidence < 1


def test_an_unmeasured_number_scores_low_not_high() -> None:
    measured = choose(selector(option("t"))).report
    unmeasured = choose(selector(option("t", quality=None, seconds=None, cost_usd=None, memory_mb=None))).report
    assert measured is not None and unmeasured is not None and unmeasured.ranking[0].score < measured.ranking[0].score
    assert set(unmeasured.ranking[0].unknown) == {"reliability", "quality", "latency", "cost", "resource"}


def test_an_estimate_moves_towards_what_was_measured_without_jumping() -> None:
    assert expected(100.0, 0.0, 0, 2.0) == 100.0 and expected(100.0, 10.0, 1, 2.0) == pytest.approx(70.0)
    assert expected(100.0, 1_000.0, 100, 2.0) == pytest.approx(1_200 / 102) and expected(100.0, 10_000.0, 100, 2.0) == pytest.approx(100.0) and expected(None, 30.0, 3, 2.0) == 10.0 and expected(None, 0.0, 0, 2.0) is None


def test_simulated_outcomes_never_count_for_a_real_tool() -> None:
    report = choose(selector(option("t")), history=history("t", 500, evidence=SIMULATED)).report
    assert report is not None and report.ranking[0].attempts == 0 and report.ranking[0].reliability == 0.5


def test_real_outcomes_never_count_for_a_simulated_tool() -> None:
    report = choose(selector(option("t", simulated=True)), history=history("t", 500, evidence=REAL)).report
    assert report is not None and report.ranking[0].attempts == 0


# --- Determinism -------------------------------------------------------------------------------------------


def test_the_same_inputs_always_give_the_same_ranking() -> None:
    options = [option(f"t{n}", quality=n / 10) for n in range(6)]
    results = [choose(selector(*options), history=history("t2", 3, 1)) for _ in range(20)]
    assert len({json.dumps(r.report.to_dict(), sort_keys=True) for r in results if r.report}) == 1


def test_a_tie_goes_to_the_configured_order() -> None:
    assert choose(selector(option("second"), option("first"))).provider == "second"
    assert choose(selector(option("first"), option("second"))).provider == "first"


def test_a_difference_of_floating_point_noise_is_a_tie() -> None:
    lower, higher = option("lower", quality=0.3), option("higher", quality=0.3 + 1e-11)
    report = choose(selector(lower, higher)).report
    assert report is not None and report.ranking[0].tool_id == "lower" and report.ranking[1].score > report.ranking[0].score  # noise, so the configured order decides
    assert choose(selector(higher, lower)).provider == "higher"


def test_the_order_given_decides_only_ties() -> None:
    good, bad = option("good", quality=0.9), option("bad", quality=0.1)
    assert choose(selector(good, bad)).provider == choose(selector(bad, good)).provider == "good"


def test_two_tools_may_not_share_a_name() -> None:
    with pytest.raises(ValueError, match="share a name"):
        selector(option("same"), option("same"))


def test_a_tool_with_no_matching_profile_is_refused_at_startup() -> None:
    with pytest.raises(ValueError, match="no matching profile"):
        selector(ProviderOption("bare", Launcher(), frozenset({CAPABILITY})))  # type: ignore[arg-type]


# --- Budget ------------------------------------------------------------------------------------------------


def test_a_tool_the_budget_cannot_start_is_refused() -> None:
    with pytest.raises(NoProvider, match=r"the run has \$0\.0500 left"):
        choose(selector(option("t", min_cost_usd=0.1, cost_usd=None)), remaining_usd=0.05)


def test_exactly_enough_budget_is_enough() -> None:
    assert choose(selector(option("t", min_cost_usd=0.1, cost_usd=None)), remaining_usd=0.1).provider == "t"


def test_a_tool_is_refused_on_what_it_has_really_been_costing() -> None:
    costly = history("t", 4, cost_total=8.0, cost_samples=4)  # measured $2 an attempt, against a declared $0.25
    with pytest.raises(NoProvider, match="an attempt needs about"):
        choose(selector(option("t")), remaining_usd=0.5, history=costly)


def test_an_unknown_cost_is_refused_under_a_spending_limit() -> None:
    with pytest.raises(NoProvider, match="cost is not known"):
        choose(selector(option("t", cost_usd=None, min_cost_usd=None)), remaining_usd=100.0)


def test_with_no_spending_limit_an_unknown_cost_is_allowed() -> None:
    assert choose(selector(option("t", cost_usd=None, min_cost_usd=None)), remaining_usd=None).provider == "t"


def test_a_cheaper_tool_is_used_when_the_budget_rules_out_the_better_one() -> None:
    chooser = selector(option("dear", quality=1.0, cost_usd=0.9, min_cost_usd=0.5), option("cheap", quality=0.2, cost_usd=0.05, min_cost_usd=0.01))
    assert choose(chooser).provider == "dear" and choose(chooser, remaining_usd=0.2).provider == "cheap"


def test_too_few_tokens_are_refused() -> None:
    with pytest.raises(NoProvider, match="at least 25000 tokens"):
        choose(selector(option("t", min_tokens=25_000)), remaining_tokens=24_999)


def test_exhausted_budget_stops_cleanly() -> None:
    with pytest.raises(NoProvider):
        choose(selector(option("t")), remaining_usd=0.0, remaining_tokens=0)


# --- Memory ------------------------------------------------------------------------------------------------


def test_too_little_memory_is_refused() -> None:
    with pytest.raises(NoProvider, match="the machine can commit 500 MB"):
        choose(selector(option("t", memory_mb=2_048), memory=500))


def test_unknown_available_memory_refuses_nothing() -> None:
    assert choose(selector(option("t", memory_mb=2_048), memory=None)).provider == "t"


def test_the_memory_probe_answers_on_this_machine() -> None:
    measured = available_memory_mb()
    assert measured is None or measured >= 0


# --- Real and simulated ------------------------------------------------------------------------------------


def test_a_simulation_never_replaces_a_real_tool_however_it_scores() -> None:
    chooser = selector(option("real", quality=0.0, seconds=9_000.0), option("sim", simulated=True, **STRONG))
    selection = choose(chooser, history=history("sim", 500, evidence=SIMULATED))
    assert selection.provider == "real" and selection.simulated is False and Refusal.SIMULATED in codes(chooser, "sim")


def test_a_simulation_is_used_only_when_nothing_real_can_run() -> None:
    selection = choose(selector(option("real", reason="docker is down"), option("sim", simulated=True)))
    assert selection.provider == "sim" and selection.simulated is True


def test_a_deployment_can_forbid_simulations_altogether() -> None:
    policy = EligibilityPolicy(approved_environments=frozenset({"docker-internal"}), allow_simulated=False)
    with pytest.raises(NoProvider, match="does not allow simulated tools"):
        choose(selector(option("sim", simulated=True), policy=policy))


def test_one_real_tool_is_not_presented_as_a_comparison() -> None:
    report = choose(selector(option("only"))).report
    assert report is not None and report.compared is False and report.real_candidates == 1 and report.margin is None and "nothing was compared" in report.explanation


def test_simulations_compared_together_do_not_count_as_real_candidates() -> None:
    report = choose(selector(option("a", simulated=True), option("b", simulated=True))).report
    assert report is not None and report.compared is True and report.real_candidates == 0


# --- Safe fallback -----------------------------------------------------------------------------------------


def test_after_a_failure_another_eligible_tool_is_tried_first() -> None:
    chooser = selector(option("best", quality=1.0), option("other", quality=0.1))
    selection = choose(chooser, failed_here=frozenset({"best"}))
    assert selection.provider == "other" and selection.report is not None and selection.report.fallback == "switched"


def test_with_no_alternative_the_same_tool_may_try_again() -> None:
    report = choose(selector(option("only")), failed_here=frozenset({"only"})).report
    assert report is not None and report.fallback == "retried_same" and "attempt limit" in report.explanation


def test_a_failed_tool_is_ranked_last_not_removed() -> None:
    report = choose(selector(option("a", quality=1.0), option("b", quality=0.5), option("c", quality=0.1)), failed_here=frozenset({"a"})).report
    assert report is not None and [c.tool_id for c in report.ranking] == ["b", "c", "a"]


def test_an_alternative_that_is_not_eligible_is_never_a_fallback() -> None:
    chooser = selector(option("only"), option("unsafe", isolated=False), option("down", reason="docker is down"))
    selection = choose(chooser, failed_here=frozenset({"only"}))
    assert selection.provider == "only" and selection.report is not None and selection.report.fallback == "retried_same"


def test_a_fallback_also_respects_the_budget() -> None:
    chooser = selector(option("cheap", min_cost_usd=0.01, cost_usd=0.01), option("dear", min_cost_usd=5.0))
    selection = choose(chooser, failed_here=frozenset({"cheap"}), remaining_usd=0.5)
    assert selection.provider == "cheap" and selection.report is not None and selection.report.fallback == "retried_same"


def test_with_no_failure_there_is_no_fallback() -> None:
    report = choose(selector(option("a"))).report
    assert report is not None and report.fallback is None


# --- The report --------------------------------------------------------------------------------------------


def test_the_report_returns_ranking_breakdown_and_rejections() -> None:
    report = choose(selector(option("a", quality=0.9), option("b", quality=0.2), option("c", isolated=False))).report
    assert report is not None
    data = report.to_dict()
    assert [r["tool"] for r in data["ranking"]] == ["a", "b"] and set(data["ranking"][0]["breakdown"]) == {"capability", "reliability", "quality", "latency", "cost", "resource"}
    assert data["rejected"][0]["tool"] == "c" and data["rejected"][0]["codes"] == ["SECURITY"] and data["margin"] > 0
    assert json.loads(json.dumps(data)) == data


def test_gpt_gets_the_explanation_without_the_full_ranking() -> None:
    selection = choose(selector(option("a"), option("b")))
    assert set(selection.summary()) == {"policy", "compared", "real_candidates", "score", "confidence", "fallback", "explanation"} and "ranking" in selection.details()


# --- Fixed preference stays available ----------------------------------------------------------------------


def test_the_fixed_policy_ignores_history_as_it_always_did() -> None:
    fixed = ProviderSelector([option("first"), option("second")])
    assert fixed.choose(CAPABILITY, context(history=history("first", 0, 50) | history("second", 50))).provider == "first"
    assert fixed.choose(CAPABILITY).summary() == {"policy": "fixed"}


def test_both_policies_have_the_same_interface() -> None:
    options = [option("a"), option("b")]
    for chooser in (ProviderSelector(options), selector(*options)):
        assert chooser.first_launcher() is options[0].launcher and isinstance(chooser.choose(CAPABILITY), Selection)


# --- What really exists ------------------------------------------------------------------------------------


def _deployed(tmp_path: Path, **changes: Any) -> tuple[Any, EgressPolicy]:
    return make_settings(tmp_path, **changes), EgressPolicy.from_file(ALLOWLIST_FILE)


def test_the_policy_setting_chooses_the_selector(tmp_path: Path) -> None:
    settings, egress = _deployed(tmp_path)
    assert isinstance(build_selector(settings, egress, [option("a")]), AdaptiveSelector)
    settings, egress = _deployed(tmp_path, selection_policy="fixed")
    assert isinstance(build_selector(settings, egress, [option("a")]), ProviderSelector)


def test_opencode_claims_only_what_has_been_shown(tmp_path: Path) -> None:
    settings, _ = _deployed(tmp_path)
    found = opencode_profile(settings)
    assert found.status is ToolStatus.IMPLEMENTED and set(found.capabilities) == CODING_CAPABILITIES
    # python-cli is shown by real runs at full weight; static-html is declared at a reduced weight until a real website build proves it.
    assert dict(found.stacks) == {"python-cli": 1.0, "static-html": 0.6}
    assert found.quality is None and found.seconds is None and found.cost_usd is None  # no real model has run through it


def test_opencode_is_refused_without_the_key_or_the_network_approval(tmp_path: Path) -> None:
    settings, egress = _deployed(tmp_path, openai_api_key=None, coding_live_approved=False)
    tool = ProviderOption(OPENCODE_NAME, Launcher(), CODING_CAPABILITIES, profile=opencode_profile(settings))  # type: ignore[arg-type]
    chooser = AdaptiveSelector([tool], policy=eligibility_policy(settings, egress), memory_probe=lambda: None)
    with pytest.raises(NoProvider) as caught:
        choose(chooser)
    assert "OPENAI_API_KEY" in caught.value.excluded[OPENCODE_NAME] and "api.openai.com" in caught.value.excluded[OPENCODE_NAME]


def test_opencode_is_eligible_only_once_a_person_approved_live_use(tmp_path: Path) -> None:
    settings, egress = _deployed(tmp_path, coding_live_approved=True)
    policy = eligibility_policy(settings, egress)
    assert policy.configured_credentials == {"OPENAI_API_KEY"} and policy.approved_network == {"api.openai.com"}


def test_the_policy_holds_names_and_never_a_credential(tmp_path: Path) -> None:
    settings, egress = _deployed(tmp_path)
    assert settings.openai_api_key is not None and settings.openai_api_key.get_secret_value() not in repr(eligibility_policy(settings, egress))


def test_the_budget_floor_is_the_reservation_measured_on_real_containers(tmp_path: Path) -> None:
    settings, _ = _deployed(tmp_path, openai_price_input_per_mtok=2.0, openai_price_output_per_mtok=10.0, gateway_max_output_tokens=8_000)
    assert opencode_profile(settings).min_cost_usd == pytest.approx(0.1144, abs=0.0001)


def test_with_no_prices_opencode_has_no_known_floor(tmp_path: Path) -> None:
    settings, _ = _deployed(tmp_path, openai_price_input_per_mtok=None, openai_price_output_per_mtok=None)
    assert opencode_profile(settings).min_cost_usd is None


def test_the_simulated_profile_says_nothing_about_quality() -> None:
    assert SIMULATED_PROFILE.quality is None and math.isclose(SIMULATED_PROFILE.cost_usd or 0.0, 0.0)
