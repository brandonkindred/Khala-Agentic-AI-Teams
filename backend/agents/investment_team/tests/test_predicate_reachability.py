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
