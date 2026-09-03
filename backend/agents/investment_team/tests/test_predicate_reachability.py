"""Unit tests for the pre-backtest ``PredicateReachabilityProbe``.

Synthetic OHLCV series with known shapes drive each reachability verdict:
a monotonically rising series makes a fast/slow SMA cross never happen, so a
``sma(fast) < sma(slow)`` entry is provably data-dependent dead code, while a
``close > sma(slow)`` entry always holds.
"""

from __future__ import annotations

from investment_team.market_data_service import OHLCVBar
from investment_team.models import StrategySpec
from investment_team.strategy_lab.quality_gates.predicate_reachability import (
    PredicateReachabilityProbe,
)
from investment_team.strategy_lab.spec_dsl import (
    DEFAULT_SIZING_PAYLOAD,
    AllOf,
    AnyOf,
    EntryRule,
    IndicatorRef,
    Predicate,
    StopLossRule,
)


def _rising_bars(n: int = 300) -> list[OHLCVBar]:
    """Monotonically rising closes — a fast SMA stays above a slow SMA forever."""
    return [
        OHLCVBar(
            date=f"2020-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
            open=100.0 + i,
            high=101.0 + i,
            low=99.0 + i,
            close=100.0 + i,
            volume=1000.0 + i,
        )
        for i in range(n)
    ]


def _spec(when, *, custom: bool = False, extra_entries=None) -> StrategySpec:
    entries = [EntryRule(side="long", when=when)]
    entries += list(extra_entries or [])
    spec = StrategySpec(
        strategy_id="t",
        authored_by="x",
        asset_class="stocks",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
        entry_rules=entries,
        exit_rules=[StopLossRule(pct=0.05)],
        sizing=DEFAULT_SIZING_PAYLOAD,
        target_symbols=["AAA"],
    )
    return spec.model_copy(update={"requires_custom_code": custom})


def _sma(period: int) -> IndicatorRef:
    return IndicatorRef(name="sma", params={"period": period})


_MD = {"AAA": _rising_bars()}
_DEAD = Predicate(lhs=_sma(5), op="<", rhs=_sma(200))  # fast<slow never on a rising series
_ALIVE = Predicate(lhs="bar.close", op=">", rhs=_sma(200))  # close>slow always on a rising series


def _details(results):
    return [(r.severity, r.details) for r in results]


def test_reachable_predicate_is_info() -> None:
    probe = PredicateReachabilityProbe()
    reach = probe.probe(_spec(_ALIVE), _MD)
    results = probe.to_gate_results(reach, _spec(_ALIVE))
    assert all(r.severity == "info" for r in results), _details(results)
    assert probe.all_entries_dead(reach) is False


def test_dead_predicate_compiled_is_critical() -> None:
    probe = PredicateReachabilityProbe()
    spec = _spec(_DEAD)
    reach = probe.probe(spec, _MD)
    results = probe.to_gate_results(reach, spec)
    crit = [r for r in results if r.severity == "critical"]
    assert crit and "never satisfies" in crit[0].details
    assert crit[0].rule_id == "entry[0]"
    assert probe.all_entries_dead(reach) is True


def test_dead_predicate_custom_is_warning_not_critical() -> None:
    probe = PredicateReachabilityProbe()
    spec = _spec(_DEAD, custom=True)
    results = probe.to_gate_results(probe.probe(spec, _MD), spec)
    assert any(r.severity == "warning" for r in results)
    assert not any(r.severity == "critical" for r in results)


def test_all_of_with_one_leg_never_holds_diagnostic() -> None:
    # One leg is always true and one is always false on a rising series, so the
    # conjunction is dead because a leg never holds ON ITS OWN — exercises the
    # "never hold on their own" diagnostic branch (the "never co-occur" branch,
    # where every leg fires but never together, is covered by
    # ``test_leg_diagnostic_never_co_occur_branch``).
    never_holds = AllOf(
        of=[
            Predicate(lhs="bar.close", op=">", rhs=_sma(200)),  # always true
            Predicate(lhs="bar.close", op="<", rhs=_sma(200)),  # always false
        ]
    )
    probe = PredicateReachabilityProbe()
    spec = _spec(never_holds)
    results = probe.to_gate_results(probe.probe(spec, _MD), spec)
    crit = [r for r in results if r.severity == "critical"]
    assert crit, _details(results)
    # The false leg never holds on its own → "never hold on their own" branch.
    assert "never hold on their own" in crit[0].details


def test_leg_diagnostic_never_co_occur_branch() -> None:
    # Direct unit test of the diagnostic: when every conjunct fires on its own but
    # the whole rule never does, report the unsatisfiable-conjunction message.
    from investment_team.strategy_lab.quality_gates.predicate_reachability import (
        _leg_diagnostic,
        _LegReachability,
        _RuleReachability,
    )

    r = _RuleReachability(
        rule_index=0,
        side="long",
        evaluated=100,
        fires=0,
        legs=(
            _LegReachability("A>B", evaluated=100, fires=40),
            _LegReachability("C>D", evaluated=100, fires=60),
        ),
    )
    assert "never co-occur" in _leg_diagnostic(r)


def test_insufficient_bars_abstains_with_info() -> None:
    probe = PredicateReachabilityProbe()
    spec = _spec(_DEAD)
    short_md = {"AAA": _rising_bars(30)}  # < 200 warmup → almost no post-warmup bars
    results = probe.to_gate_results(probe.probe(spec, short_md), spec)
    # No critical: too few post-warmup bars to call it dead code.
    assert not any(r.severity == "critical" for r in results)
    assert any("too few to judge" in r.details for r in results)


def test_no_entry_rules_or_no_data_returns_empty_probe() -> None:
    probe = PredicateReachabilityProbe()
    assert probe.probe(_spec(_ALIVE), None) == []
    assert probe.probe(_spec(_ALIVE), {}) == []
    assert probe.probe(_spec(_ALIVE), {"AAA": []}) == []


def test_all_entries_dead_requires_every_rule_dead() -> None:
    probe = PredicateReachabilityProbe()
    # One dead + one alive → not all dead → no forced-redesign signal.
    spec = _spec(_DEAD, extra_entries=[EntryRule(side="long", when=_ALIVE)])
    reach = probe.probe(spec, _MD)
    assert probe.all_entries_dead(reach) is False
    # Both dead → all dead.
    both_dead = _spec(
        _DEAD,
        extra_entries=[EntryRule(side="long", when=Predicate(lhs=_sma(3), op="<", rhs=_sma(200)))],
    )
    assert probe.all_entries_dead(probe.probe(both_dead, _MD)) is True


def test_check_convenience_wraps_probe_and_format() -> None:
    probe = PredicateReachabilityProbe()
    spec = _spec(_DEAD)
    results = probe.check(spec, _MD, phase="synthesis")
    assert all(
        r.phase == "synthesis" and r.gate_name == "predicate_reachability_probe" for r in results
    )
    assert any(r.severity == "critical" for r in results)


# ---------------------------------------------------------------------------
# Additional shapes: partial reachability, cross_above, any_of, multi-symbol,
# and the exact _MIN_EVALUATED_BARS boundary.
# ---------------------------------------------------------------------------


def _v_shaped_bars(n: int = 120) -> list[OHLCVBar]:
    """Decline for the first half, then rebound steeply — a fast SMA starts
    below a slow SMA and crosses above it exactly once during the rebound."""
    bars = []
    px = 200.0
    for i in range(n):
        px += -1.0 if i < n // 2 else 3.0
        bars.append(
            OHLCVBar(
                date=f"2020-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
                open=px,
                high=px + 1,
                low=px - 1,
                close=px,
                volume=1000.0,
            )
        )
    return bars


def _oscillating_bars(n: int = 200) -> list[OHLCVBar]:
    """A sine-wave close series — an RSI threshold fires on roughly half the bars."""
    import math

    return [
        OHLCVBar(
            date=f"2020-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0 + 10 * math.sin(i / 5.0),
            volume=1000.0,
        )
        for i in range(n)
    ]


def _declining_bars(n: int = 300) -> list[OHLCVBar]:
    """Monotonically declining closes — the mirror of ``_rising_bars``."""
    return [
        OHLCVBar(
            date=f"2020-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
            open=500.0 - i,
            high=501.0 - i,
            low=499.0 - i,
            close=500.0 - i,
            volume=1000.0,
        )
        for i in range(n)
    ]


def test_partial_reachability_fires_on_some_bars_not_all() -> None:
    # RSI oscillates around 50 on a sine-wave series, so 'rsi < 50' fires on
    # roughly (but not exactly) half the post-warmup bars — neither always-true
    # nor always-false.
    when = Predicate(lhs=IndicatorRef(name="rsi", params={"period": 14}), op="<", rhs=50.0)
    probe = PredicateReachabilityProbe()
    spec = _spec(when)
    reach = probe.probe(spec, {"AAA": _oscillating_bars()})
    assert reach[0].judged
    assert 0 < reach[0].fires < reach[0].evaluated, reach[0]


def test_cross_above_fires_exactly_at_the_crossing_bar() -> None:
    # A fast SMA starts below a slow SMA (decline) and crosses above it exactly
    # once during the rebound — cross_above depends on previous-bar state, the
    # operator most likely to diverge between the probe's loop and the engine's.
    when = Predicate(
        lhs=IndicatorRef(name="sma", params={"period": 3}),
        op="cross_above",
        rhs=IndicatorRef(name="sma", params={"period": 10}),
    )
    probe = PredicateReachabilityProbe()
    spec = _spec(when)
    reach = probe.probe(spec, {"AAA": _v_shaped_bars()})
    assert reach[0].judged
    assert reach[0].fires == 1, reach[0]  # crosses exactly once
    results = probe.to_gate_results(reach, spec)
    assert all(r.severity == "info" for r in results)  # reachable, not dead


def test_any_of_reachable_when_one_branch_is_alive() -> None:
    # One branch never holds, the other always does — the any_of as a whole
    # must be reachable (every bar satisfies at least the alive branch).
    when = AnyOf(
        of=[
            Predicate(lhs="bar.close", op="<", rhs=_sma(200)),  # always false
            Predicate(lhs="bar.close", op=">", rhs=_sma(200)),  # always true
        ]
    )
    probe = PredicateReachabilityProbe()
    spec = _spec(when)
    reach = probe.probe(spec, _MD)
    assert reach[0].fires == reach[0].evaluated  # fires on every judged bar
    assert not any(r.severity == "critical" for r in probe.to_gate_results(reach, spec))


def test_multi_symbol_mixed_reachability_aggregates_across_symbols() -> None:
    # AAA never satisfies the predicate (declining); BBB always does (rising).
    # The rule as a whole is reachable because it fires on BBB's bars, and the
    # evaluated count aggregates both symbols' post-warmup bars.
    when = Predicate(lhs="bar.close", op=">", rhs=_sma(200))
    probe = PredicateReachabilityProbe()
    spec = _spec(when)
    reach = probe.probe(spec, {"AAA": _declining_bars(), "BBB": _rising_bars()})
    assert reach[0].judged
    assert 0 < reach[0].fires < reach[0].evaluated
    assert not any(r.severity == "critical" for r in probe.to_gate_results(reach, spec))


def test_min_evaluated_bars_boundary_19_vs_20() -> None:
    # sma(period=5) values start at index 4 (0-indexed), so N bars yields
    # evaluated = N - 4. N=23 -> evaluated=19 (below threshold, unjudged);
    # N=24 -> evaluated=20 (at threshold, judged) — the exact abstention edge.
    when = Predicate(lhs="bar.close", op=">", rhs=_sma(5))
    probe = PredicateReachabilityProbe()
    spec = _spec(when)

    below = probe.probe(spec, {"AAA": _rising_bars(23)})
    assert below[0].evaluated == 19
    assert below[0].judged is False
    below_results = probe.to_gate_results(below, spec)
    assert not any(r.severity == "critical" for r in below_results)
    assert any("too few to judge" in r.details for r in below_results)

    at = probe.probe(spec, {"AAA": _rising_bars(24)})
    assert at[0].evaluated == 20
    assert at[0].judged is True


# ---------------------------------------------------------------------------
# probe_pairs: pairwise entry-predicate co-occurrence analysis.
# ---------------------------------------------------------------------------


def test_pair_later_fires_independently_of_earlier() -> None:
    # Disjoint firing sets (no indicators, so neither predicate warms up):
    # earlier fires on the early bars, later fires on the late bars.
    earlier = Predicate(lhs="bar.close", op="<", rhs=150.0)  # fires i in [0, 49]
    later = Predicate(lhs="bar.close", op=">", rhs=250.0)  # fires i in [151, 299]
    probe = PredicateReachabilityProbe()
    spec = _spec(earlier, extra_entries=[EntryRule(side="long", when=later)])
    pairs = probe.probe_pairs(spec, _MD)
    assert len(pairs) == 1
    pair = pairs[0]
    assert pair.earlier_index == 0 and pair.later_index == 1
    assert pair.judged
    assert pair.later_fires > 0
    assert pair.later_independent_fires == pair.later_fires
    assert pair.later_never_independent is False
    assert pair.later_dead is False


def test_pair_later_never_fires_independently_of_earlier() -> None:
    # _ALIVE (close>sma(200)) is judged (and always fires) only from i=199 on
    # (sma(200) warmup). A later rule whose firing window sits entirely inside
    # that judged range can never fire independently of it.
    later = Predicate(lhs="bar.close", op=">", rhs=310.0)  # fires i > 210
    probe = PredicateReachabilityProbe()
    spec = _spec(_ALIVE, extra_entries=[EntryRule(side="long", when=later)])
    pairs = probe.probe_pairs(spec, _MD)
    assert len(pairs) == 1
    pair = pairs[0]
    assert pair.judged
    assert pair.later_fires > 0
    assert pair.later_independent_fires == 0
    assert pair.later_never_independent is True
    assert pair.later_dead is False


def test_pair_later_dead_is_distinct_from_never_independent() -> None:
    # A later rule that never fires at all is "dead" (already reported
    # elsewhere) — the pairwise analysis must not also call it "never
    # independent", which is reserved for a rule that fires but is shadowed.
    probe = PredicateReachabilityProbe()
    spec = _spec(_ALIVE, extra_entries=[EntryRule(side="long", when=_DEAD)])
    pairs = probe.probe_pairs(spec, _MD)
    assert len(pairs) == 1
    pair = pairs[0]
    assert pair.judged
    assert pair.later_fires == 0
    assert pair.later_dead is True
    assert pair.later_never_independent is False


def test_pair_leg_diagnostics_decompose_later_rule_two_leg_all_of() -> None:
    # Both legs' firing windows sit entirely inside _ALIVE's judged, always-
    # firing range, so the all_of as a whole (and each leg individually) never
    # fires independently of it — exercising the per-leg breakdown.
    later = AllOf(
        of=[
            Predicate(lhs="bar.close", op=">", rhs=300.0),  # fires i > 200
            Predicate(lhs="bar.close", op="<", rhs=360.0),  # fires i < 260
        ]
    )
    probe = PredicateReachabilityProbe()
    spec = _spec(_ALIVE, extra_entries=[EntryRule(side="long", when=later)])
    pairs = probe.probe_pairs(spec, _MD)
    assert len(pairs) == 1
    pair = pairs[0]
    assert pair.later_never_independent is True
    assert len(pair.legs) == 2
    for leg in pair.legs:
        assert leg.evaluated == pair.evaluated
        assert leg.fires > 0
        assert leg.independent_fires == 0


def test_pair_mixed_side_still_pairs() -> None:
    probe = PredicateReachabilityProbe()
    spec = _spec(_ALIVE, extra_entries=[EntryRule(side="short", when=_DEAD)])
    pairs = probe.probe_pairs(spec, _MD)
    assert len(pairs) == 1
    pair = pairs[0]
    assert pair.earlier_side == "long"
    assert pair.later_side == "short"


def test_pair_fewer_than_two_rules_returns_empty() -> None:
    probe = PredicateReachabilityProbe()
    assert probe.probe_pairs(_spec(_ALIVE), _MD) == []


def test_pair_no_market_data_returns_empty() -> None:
    probe = PredicateReachabilityProbe()
    spec = _spec(_ALIVE, extra_entries=[EntryRule(side="long", when=_DEAD)])
    assert probe.probe_pairs(spec, None) == []
    assert probe.probe_pairs(spec, {}) == []
    assert probe.probe_pairs(spec, {"AAA": []}) == []


def test_pair_min_evaluated_bars_boundary_19_vs_20() -> None:
    # Neither predicate involves an indicator, so evaluated == the bar count
    # exactly (no warmup on either side) — the exact abstention edge.
    earlier = Predicate(lhs="bar.close", op=">", rhs=0.0)
    later = Predicate(lhs="bar.close", op=">", rhs=50.0)
    probe = PredicateReachabilityProbe()
    spec = _spec(earlier, extra_entries=[EntryRule(side="long", when=later)])

    below = probe.probe_pairs(spec, {"AAA": _rising_bars(19)})
    assert below[0].evaluated == 19
    assert below[0].judged is False

    at = probe.probe_pairs(spec, {"AAA": _rising_bars(20)})
    assert at[0].evaluated == 20
    assert at[0].judged is True


def test_cooccurrence_counts_pure_function_hand_built() -> None:
    # Direct unit test of the pure computation: no probe, spec, or bars
    # involved — just two hand-built, positionally-aligned status sequences.
    from investment_team.strategy_lab.quality_gates.predicate_reachability import (
        _cooccurrence_counts,
    )

    later_statuses = ["satisfied", "satisfied", "warmup", "miss"]
    earlier_statuses = ["miss", "satisfied", "satisfied", "miss"]
    # Bar 2 excluded (later is warmup there). Of the remaining 3: later fires
    # at 0 and 1 (evaluated=3, later_fires=2); of those, only bar 0 has the
    # earlier rule not satisfied (later_independent_fires=1).
    assert _cooccurrence_counts(later_statuses, earlier_statuses) == (3, 2, 1)


def test_cooccurrence_counts_pure_function_excludes_earlier_side_warmup() -> None:
    # Same pure function, but pinning the OTHER side of the warmup contract:
    # a bar where the EARLIER rule is warmup (not the later one) must also be
    # excluded from `evaluated`, not silently counted as an independent fire.
    from investment_team.strategy_lab.quality_gates.predicate_reachability import (
        _cooccurrence_counts,
    )

    later_statuses = ["satisfied", "satisfied", "satisfied"]
    earlier_statuses = ["warmup", "satisfied", "miss"]
    # Bar 0 excluded (earlier is warmup there). Of the remaining 2: later
    # fires at both (evaluated=2, later_fires=2); of those, only bar 2 has
    # the earlier rule not satisfied (later_independent_fires=1).
    assert _cooccurrence_counts(later_statuses, earlier_statuses) == (2, 2, 1)


def test_sweep_statuses_matches_sweep_aggregate() -> None:
    # Guards the _sweep refactor: its (evaluated, fires) counts must still
    # match what _sweep_statuses's raw per-bar sequence implies.
    from investment_team.strategy_lab.quality_gates.predicate_reachability import (
        _build_views,
        _sweep,
        _sweep_statuses,
    )

    views = _build_views(_MD)
    statuses = _sweep_statuses(_ALIVE, views)
    evaluated = sum(1 for s in statuses if s != "warmup")
    fires = sum(1 for s in statuses if s == "satisfied")
    assert _sweep(_ALIVE, views) == (evaluated, fires)
