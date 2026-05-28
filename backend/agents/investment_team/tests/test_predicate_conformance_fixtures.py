"""Unit tests for :mod:`predicate_conformance_fixtures`.

Verifies that the fixture generator produces bars exercising both
true and false predicate states, and that the engine evaluator's
verdicts are correctly stamped onto each fixture.
"""

from __future__ import annotations

from investment_team.models import StrategySpec
from investment_team.strategy_lab.quality_gates.predicate_conformance_fixtures import (
    generate_conformance_fixtures,
)
from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    IndicatorRef,
    Predicate,
    SignalExitRule,
    StopLossRule,
    TakeProfitRule,
)


def _spec(*, entry_rules=None, exit_rules=None) -> StrategySpec:
    return StrategySpec(
        strategy_id="fixture-test",
        authored_by="test",
        asset_class="stocks",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
        entry_rules=entry_rules or [],
        exit_rules=exit_rules or [],
        target_symbols=["TEST"],
    )


def _pred_close_gt_50() -> Predicate:
    return Predicate(lhs="bar.close", op=">", rhs=50.0)


def _pred_sma_gt_number() -> Predicate:
    return Predicate(
        lhs=IndicatorRef(name="sma", params={"period": 10}),
        op=">",
        rhs=100.0,
    )


def _pred_cross_above() -> Predicate:
    return Predicate(lhs="bar.close", op="cross_above", rhs=100.0)


def _pred_rsi_lt_30() -> Predicate:
    return Predicate(
        lhs=IndicatorRef(name="rsi", params={"period": 14}),
        op="<",
        rhs=30.0,
    )


class TestGenerateConformanceFixtures:
    def test_entry_rule_produces_fixture(self):
        spec = _spec(entry_rules=[EntryRule(when=_pred_close_gt_50(), side="long")])
        fixtures = generate_conformance_fixtures(spec)
        assert len(fixtures) == 1
        f = fixtures[0]
        assert f.rule_id == "entry[0]"
        assert f.rule_kind == "entry"
        assert f.side == "long"

    def test_signal_exit_rule_produces_fixture(self):
        spec = _spec(exit_rules=[SignalExitRule(when=_pred_close_gt_50())])
        fixtures = generate_conformance_fixtures(spec)
        assert len(fixtures) == 1
        assert fixtures[0].rule_kind == "signal_exit"

    def test_stop_loss_excluded(self):
        spec = _spec(exit_rules=[StopLossRule(pct=0.05)])
        fixtures = generate_conformance_fixtures(spec)
        assert len(fixtures) == 0

    def test_take_profit_excluded(self):
        spec = _spec(exit_rules=[TakeProfitRule(pct=0.1)])
        fixtures = generate_conformance_fixtures(spec)
        assert len(fixtures) == 0


class TestFixtureHasBothStates:
    def test_price_vs_number_has_both_states(self):
        spec = _spec(entry_rules=[EntryRule(when=_pred_close_gt_50(), side="long")])
        fixtures = generate_conformance_fixtures(spec)
        f = fixtures[0]
        assert f.synthesizable
        assert any(v is True for v in f.expected_verdicts)
        assert any(v is False for v in f.expected_verdicts)

    def test_sma_vs_number_has_both_states(self):
        spec = _spec(entry_rules=[EntryRule(when=_pred_sma_gt_number(), side="long")])
        fixtures = generate_conformance_fixtures(spec)
        f = fixtures[0]
        assert f.synthesizable
        assert any(v is True for v in f.expected_verdicts)
        assert any(v is False for v in f.expected_verdicts)

    def test_cross_above_has_crossing_events(self):
        spec = _spec(entry_rules=[EntryRule(when=_pred_cross_above(), side="long")])
        fixtures = generate_conformance_fixtures(spec)
        f = fixtures[0]
        assert f.synthesizable
        true_count = sum(1 for v in f.expected_verdicts if v is True)
        assert true_count >= 1, "cross_above should fire on at least 1 transition bar"
        assert any(v is False for v in f.expected_verdicts)

    def test_rsi_vs_number_oscillates(self):
        spec = _spec(entry_rules=[EntryRule(when=_pred_rsi_lt_30(), side="long")])
        fixtures = generate_conformance_fixtures(spec)
        f = fixtures[0]
        assert f.synthesizable
        assert any(v is True for v in f.expected_verdicts)
        assert any(v is False for v in f.expected_verdicts)


class TestWarmupBars:
    def test_warmup_bars_marked_none(self):
        """SMA(20) needs 20 bars before producing a value — first bars should be warmup."""
        spec = _spec(
            entry_rules=[
                EntryRule(
                    when=Predicate(
                        lhs=IndicatorRef(name="sma", params={"period": 20}),
                        op=">",
                        rhs=100.0,
                    ),
                    side="long",
                )
            ]
        )
        fixtures = generate_conformance_fixtures(spec)
        f = fixtures[0]
        if not f.synthesizable:
            # If the oscillation didn't produce both states that's a
            # known limitation — verify the unsynthesizable_reason instead.
            assert f.unsynthesizable_reason == "no_predicate_state_change"
            return
        none_count = sum(1 for v in f.expected_verdicts if v is None)
        assert none_count > 0, "SMA(20) should have warmup bars before producing values"


class TestVerdictBarAlignment:
    def test_verdicts_length_matches_bars(self):
        spec = _spec(entry_rules=[EntryRule(when=_pred_close_gt_50(), side="long")])
        fixtures = generate_conformance_fixtures(spec)
        f = fixtures[0]
        assert len(f.expected_verdicts) == len(f.bars)

    def test_uses_spec_symbol(self):
        spec = _spec(entry_rules=[EntryRule(when=_pred_close_gt_50(), side="long")])
        spec.target_symbols = ["AAPL"]
        fixtures = generate_conformance_fixtures(spec)
        assert fixtures[0].symbol == "AAPL"


class TestIndicatorVsIndicator:
    def test_two_smas_produces_fixture(self):
        pred = Predicate(
            lhs=IndicatorRef(name="sma", params={"period": 10}),
            op=">",
            rhs=IndicatorRef(name="sma", params={"period": 30}),
        )
        spec = _spec(entry_rules=[EntryRule(when=pred, side="long")])
        fixtures = generate_conformance_fixtures(spec)
        assert len(fixtures) == 1
        f = fixtures[0]
        assert f.synthesizable
