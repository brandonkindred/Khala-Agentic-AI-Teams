"""Unit tests for ``SpecReadinessGate`` (issue #540).

Eight deterministic implementability rules + the two end-to-end scenarios
named in the issue's acceptance criteria. Every rule has at least one
dedicated test that exercises its failure path.
"""

from __future__ import annotations

from typing import List

from investment_team.models import BacktestConfig, StrategySpec
from investment_team.strategy_lab.quality_gates.spec_readiness import (
    GATE,
    SpecReadinessGate,
)
from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    FixedFractionSizing,
    FixedNotionalSizing,
    IndicatorRef,
    Predicate,
    SignalExitRule,
    StopLossRule,
    TakeProfitRule,
)


def _spec(
    *,
    hypothesis: str = "RSI(14) below 30 on AAPL signals long entry.",
    entry: List | None = None,
    exit_: List | None = None,
    asset_class: str = "stocks",
    timeframe: str = "1d",
    target_symbols: List[str] | None = None,
    sizing=None,
    risk_limits: dict | None = None,
) -> StrategySpec:
    if entry is None:
        entry = [
            EntryRule(
                side="long",
                when=Predicate(
                    lhs=IndicatorRef(name="rsi", params={"period": 14}),
                    op="<",
                    rhs=30.0,
                ),
            )
        ]
    if exit_ is None:
        exit_ = [
            SignalExitRule(
                when=Predicate(
                    lhs=IndicatorRef(name="rsi", params={"period": 14}),
                    op=">",
                    rhs=70.0,
                )
            )
        ]
    if target_symbols is None:
        target_symbols = ["AAPL"]
    kwargs: dict = dict(
        strategy_id="strat-readiness-test",
        authored_by="test",
        asset_class=asset_class,
        hypothesis=hypothesis,
        signal_definition="sig",
        timeframe=timeframe,
        entry_rules=entry,
        exit_rules=exit_,
        target_symbols=target_symbols,
        risk_limits=risk_limits or {"max_position_pct": 5, "max_drawdown_pct": 10},
        speculative=False,
        strategy_code="from contract import Strategy\nclass S(Strategy):\n    def on_bar(self, ctx, bar):\n        pass\n",
    )
    if sizing is not None:
        kwargs["sizing"] = sizing
    return StrategySpec(**kwargs)


def _config() -> BacktestConfig:
    return BacktestConfig(start_date="2024-01-01", end_date="2024-06-01")


def _critical(results) -> list[str]:
    return [r.details for r in results if r.severity == "critical" and not r.passed]


# ---------------------------------------------------------------------------
# Rule 1: Universe set
# ---------------------------------------------------------------------------


def test_rule1_target_symbols_missing_when_hypothesis_names_ticker() -> None:
    spec = _spec(
        hypothesis="QQQ tends to revert to its 50-day SMA after a 2-sigma move.",
        target_symbols=[],
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    critical = _critical(results)
    assert any("QQQ" in c and "target_symbols" in c for c in critical), critical


def test_rule1_hypothesis_symbol_not_in_target_symbols() -> None:
    spec = _spec(
        hypothesis="QQQ tends to revert to its 50-day SMA after a 2-sigma move.",
        target_symbols=["AAPL"],
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    critical = _critical(results)
    assert any("QQQ" in c for c in critical), critical


def test_rule1_passes_when_no_symbols_in_hypothesis_and_no_targets() -> None:
    spec = _spec(
        hypothesis="RSI(14) below 30 signals long entry.",
        target_symbols=[],
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    rule1_failures = [
        c for c in _critical(results) if "target_symbols" in c or "Hypothesis names symbol" in c
    ]
    assert not rule1_failures, rule1_failures


# ---------------------------------------------------------------------------
# Rule 2: Entry rules non-trivial
# ---------------------------------------------------------------------------


def test_rule2_no_entry_rules_is_critical() -> None:
    spec = _spec(entry=[])
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    assert any("No entry rules" in c for c in _critical(results))


# ---------------------------------------------------------------------------
# Rule 3: Indicator validity
# ---------------------------------------------------------------------------


def test_rule3_sma_without_period_param_is_critical() -> None:
    # Bypass Pydantic validation by constructing a spec then mutating an
    # IndicatorRef's params dict — the gate must catch what slipped past.
    spec = _spec()
    ref = spec.entry_rules[0].when.lhs
    assert isinstance(ref, IndicatorRef)
    # Swap the well-formed rsi(period=14) for sma with no params dict entry
    spec.entry_rules[0].when.lhs = IndicatorRef.model_construct(
        name="sma", params={}, source="close"
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    assert any("sma" in c and "period" in c for c in _critical(results))


# ---------------------------------------------------------------------------
# Rule 4: Exit completeness
# ---------------------------------------------------------------------------


def test_rule4_no_exit_rules_is_critical() -> None:
    spec = _spec(exit_=[])
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    assert any("No exit rules" in c for c in _critical(results))


# ---------------------------------------------------------------------------
# Rule 5: Sizing realisable
# ---------------------------------------------------------------------------


def test_rule5_sizing_under_one_share_is_critical() -> None:
    spec = _spec(
        sizing=FixedNotionalSizing(notional_usd=10.0),
        target_symbols=["AAPL"],
    )
    # Default provider returns $100/share; $10 notional / $100 = 0.1 share.
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    assert any("qty=" in c and "AAPL" in c for c in _critical(results))


def test_rule5_passes_with_realistic_sizing() -> None:
    spec = _spec(
        sizing=FixedFractionSizing(fraction=0.02),
        target_symbols=["AAPL"],
    )
    # 0.02 * $100k = $2000 / $100 = 20 shares.
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    sizing_failures = [c for c in _critical(results) if "qty=" in c]
    assert not sizing_failures, sizing_failures


# ---------------------------------------------------------------------------
# Rule 6: Hypothesis–rule consistency
# ---------------------------------------------------------------------------


def test_rule6_hypothesis_mentions_indicator_not_in_rules_is_critical() -> None:
    spec = _spec(
        hypothesis="MACD bullish crossovers on AAPL signal long entries.",
        # entry rule uses RSI, not MACD
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    assert any("macd" in c.lower() for c in _critical(results))


def test_rule6_passes_when_hypothesis_and_rules_match() -> None:
    spec = _spec(
        hypothesis="RSI(14) below 30 on AAPL signals oversold conditions.",
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    rule6_failures = [c for c in _critical(results) if "Hypothesis names indicator" in c]
    assert not rule6_failures, rule6_failures


# ---------------------------------------------------------------------------
# Rule 7: Timeframe data availability
# ---------------------------------------------------------------------------


def test_rule7_intraday_timeframe_on_commodities_is_critical() -> None:
    spec = _spec(
        asset_class="commodities",
        timeframe="5m",
        target_symbols=["GLD"],
        hypothesis="GLD intraday momentum.",
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    assert any("intraday" in c and "commodities" in c for c in _critical(results))


def test_rule7_daily_timeframe_on_commodities_passes() -> None:
    spec = _spec(
        asset_class="commodities",
        timeframe="1d",
        target_symbols=["GLD"],
        hypothesis="GLD daily mean reversion.",
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    tf_failures = [c for c in _critical(results) if "intraday" in c]
    assert not tf_failures, tf_failures


# ---------------------------------------------------------------------------
# Rule 8: Risk-limit coherence
# ---------------------------------------------------------------------------


def test_rule8_stop_loss_geq_take_profit_is_critical() -> None:
    spec = _spec(
        exit_=[
            StopLossRule(pct=0.10),
            TakeProfitRule(pct=0.05),
        ],
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    assert any("stop_loss.pct" in c for c in _critical(results))


def test_rule8_max_position_pct_above_25_is_critical() -> None:
    spec = _spec(risk_limits={"max_position_pct": 30, "max_drawdown_pct": 10})
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    assert any("max_position_pct=30" in c for c in _critical(results))


# ---------------------------------------------------------------------------
# Issue #540 acceptance criteria: end-to-end vague + well-formed cases
# ---------------------------------------------------------------------------


def test_vague_spec_returns_multiple_criticals() -> None:
    """A spec that is structurally valid but vague trips multiple rules."""
    spec = _spec(
        hypothesis="enter on bullish momentum on QQQ — MACD watch",
        entry=[],  # Rule 2: no entries
        exit_=[],  # Rule 4: no exits
        target_symbols=[],  # Rule 1: QQQ named, no targets
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    critical = _critical(results)
    # Expect at least three independent criticals across the three rules.
    assert len(critical) >= 3, critical
    assert any("entry" in c.lower() for c in critical)
    assert any("exit" in c.lower() for c in critical)
    assert any("QQQ" in c for c in critical)


def test_well_formed_spec_passes() -> None:
    """RSI(14) cross_above 30, SMA(50) > SMA(200) — clean entry/exit."""
    spec = _spec(
        hypothesis="On AAPL, RSI(14) crossing above 30 with SMA(50) > SMA(200) marks long entry.",
        entry=[
            EntryRule(
                side="long",
                when=Predicate(
                    lhs=IndicatorRef(name="rsi", params={"period": 14}),
                    op="cross_above",
                    rhs=30.0,
                ),
            ),
            EntryRule(
                side="long",
                when=Predicate(
                    lhs=IndicatorRef(name="sma", params={"period": 50}),
                    op=">",
                    rhs=IndicatorRef(name="sma", params={"period": 200}),
                ),
            ),
        ],
        exit_=[
            SignalExitRule(
                when=Predicate(
                    lhs=IndicatorRef(name="rsi", params={"period": 14}),
                    op=">",
                    rhs=70.0,
                )
            ),
            StopLossRule(pct=0.03),
            TakeProfitRule(pct=0.10),
        ],
        sizing=FixedFractionSizing(fraction=0.02),
        target_symbols=["AAPL"],
        risk_limits={"max_position_pct": 5, "max_drawdown_pct": 10},
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    assert not _critical(results), _critical(results)
    # Single passing summary result
    assert any(r.gate_name == GATE and r.passed and r.severity == "info" for r in results)


# ---------------------------------------------------------------------------
# Phase tagging
# ---------------------------------------------------------------------------


def test_phase_tag_propagates_to_results() -> None:
    spec = _spec()
    design = SpecReadinessGate().validate(spec, phase="design", backtest_config=_config())
    synth = SpecReadinessGate().validate(spec, phase="synthesis", backtest_config=_config())
    assert all(r.phase == "design" for r in design)
    assert all(r.phase == "synthesis" for r in synth)


def test_custom_market_sample_provider_is_used() -> None:
    spec = _spec(
        sizing=FixedNotionalSizing(notional_usd=500.0),
        target_symbols=["AAPL"],
    )
    # Pretend AAPL is $1000 — $500 notional yields 0.5 share, should fail.
    gate = SpecReadinessGate(market_sample_provider=lambda sym: 1000.0)
    results = gate.validate(spec, backtest_config=_config())
    assert any("qty=" in c for c in _critical(results))
