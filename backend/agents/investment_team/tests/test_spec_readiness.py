"""Unit tests for ``SpecReadinessGate``.

Eight deterministic implementability rules plus the two end-to-end scenarios
that match the gate's acceptance contract. Every rule has at least one
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


def test_rule1_catches_futures_ticker_mismatch() -> None:
    """Bare futures names (`ES`, `NQ`) in the hypothesis must be caught."""
    spec = _spec(
        hypothesis="Trade ES on Monday-morning gaps.",
        target_symbols=["NQ=F"],
        asset_class="futures",
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    critical = _critical(results)
    assert any("ES" in c for c in critical), critical


def test_rule1_catches_forex_ticker_mismatch() -> None:
    """Forex suffixed tickers (`EURUSD=X`) in the hypothesis must be caught."""
    spec = _spec(
        hypothesis="EURUSD=X tends to revert intraday.",
        target_symbols=["GBPUSD=X"],
        asset_class="forex",
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    critical = _critical(results)
    assert any("EURUSD=X" in c for c in critical), critical


def test_rule1_canonicalizes_bare_futures_against_provider_suffix() -> None:
    """``ES`` in hypothesis matches ``ES=F`` in target_symbols — same symbol."""
    spec = _spec(
        hypothesis="Trade ES gaps on Monday mornings.",
        target_symbols=["ES=F"],
        asset_class="futures",
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    rule1_failures = [
        c for c in _critical(results) if "target_symbols" in c or "Hypothesis names symbol" in c
    ]
    assert not rule1_failures, rule1_failures


def test_rule1_canonicalizes_bare_forex_against_provider_suffix() -> None:
    """``EURUSD`` in hypothesis matches ``EURUSD=X`` in target_symbols."""
    spec = _spec(
        hypothesis="EURUSD tends to revert intraday.",
        target_symbols=["EURUSD=X"],
        asset_class="forex",
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    rule1_failures = [
        c for c in _critical(results) if "target_symbols" in c or "Hypothesis names symbol" in c
    ]
    assert not rule1_failures, rule1_failures


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


def test_rule5_exactly_one_whole_lot_passes_for_stocks() -> None:
    """A notional spec yielding qty=1.0 on stocks is implementable."""
    spec = _spec(
        sizing=FixedNotionalSizing(notional_usd=100.0),
        target_symbols=["AAPL"],
        asset_class="stocks",
    )
    # Default provider returns $100/share; 100 / 100 = 1.0 exactly.
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    sizing_failures = [c for c in _critical(results) if "qty=" in c]
    assert not sizing_failures, sizing_failures


def test_rule5_nan_price_fails_closed() -> None:
    """A provider returning NaN must trip Rule 5 — fail closed."""
    spec = _spec(
        sizing=FixedNotionalSizing(notional_usd=1000.0),
        target_symbols=["AAPL"],
        asset_class="stocks",
    )
    gate = SpecReadinessGate(market_sample_provider=lambda sym, asset_class: float("nan"))
    results = gate.validate(spec, backtest_config=_config())
    assert any("no usable price sample" in c for c in _critical(results))


def test_default_universe_for_futures_and_forex() -> None:
    """`_default_universe_for` must return matching asset-class symbols."""
    from investment_team.strategy_lab.quality_gates.spec_readiness import _default_universe_for
    from investment_team.symbols import FOREX_SYMBOLS, FUTURES_SYMBOLS

    assert _default_universe_for("futures") == list(FUTURES_SYMBOLS)
    assert _default_universe_for("forex") == list(FOREX_SYMBOLS)


def test_rule5_accepts_fractional_qty_on_crypto() -> None:
    """Crypto specs accept fractional positions — 0.1 BTC is implementable."""
    spec = _spec(
        asset_class="crypto",
        target_symbols=["BTC"],
        sizing=FixedNotionalSizing(notional_usd=10.0),
        hypothesis="BTC mean-reversion on the 1d timeframe.",
        # Replace the default RSI entry with a self-consistent one to avoid
        # tripping Rule 6 (hypothesis mentions reversion but no rsi term).
    )
    # Default provider returns $100/BTC → 0.1 BTC. Crypto allows fractional, pass.
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    sizing_failures = [c for c in _critical(results) if "qty=" in c]
    assert not sizing_failures, sizing_failures


def test_rule5_accepts_fractional_qty_on_forex() -> None:
    """Forex specs accept fractional positions — sub-lot sizing is valid."""
    spec = _spec(
        asset_class="forex",
        target_symbols=["EURUSD=X"],
        sizing=FixedNotionalSizing(notional_usd=50.0),
        hypothesis="EURUSD=X mean-reverts intraday on RSI(14).",
        timeframe="1h",
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    sizing_failures = [c for c in _critical(results) if "qty=" in c]
    assert not sizing_failures, sizing_failures


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


def test_rule1_matches_lowercase_ticker_in_hypothesis() -> None:
    """Rule 1's regex is case-insensitive: `qqq` in hypothesis is caught."""
    spec = _spec(
        hypothesis="qqq mean-reverts to its 50-day moving average.",
        target_symbols=[],
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    assert any("QQQ" in c for c in _critical(results))


def test_rule6_moving_average_is_satisfied_by_ema() -> None:
    """'Moving average' in the hypothesis is satisfied by either SMA or EMA."""
    spec = _spec(
        hypothesis="AAPL crosses its EMA moving average to signal long entry.",
        entry=[
            EntryRule(
                side="long",
                when=Predicate(
                    lhs=IndicatorRef(name="ema", params={"period": 20}),
                    op="cross_above",
                    rhs="bar.close",
                ),
            ),
        ],
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    rule6_failures = [c for c in _critical(results) if "moving average" in c]
    assert not rule6_failures, rule6_failures


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
# Acceptance contract: end-to-end vague + well-formed cases
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


def test_orchestrator_wires_readiness_price_provider() -> None:
    """``StrategyLabOrchestrator`` constructs the gate with a real-price provider.

    The provider invokes ``MarketDataService.fetch_ohlcv``; we verify the
    wiring by monkeypatching the service to return a sentinel close and
    confirming Rule 5 uses that price.
    """
    from investment_team.market_data_service import OHLCVBar
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    orch = StrategyLabOrchestrator()

    sentinel_bar = OHLCVBar(
        date="2024-06-01",
        open=950.0,
        high=960.0,
        low=940.0,
        close=950.0,
        volume=1_000_000,
    )
    orch.market_data_service.fetch_ohlcv = lambda symbol, asset_class, days=5: [sentinel_bar]

    spec = _spec(
        sizing=FixedNotionalSizing(notional_usd=500.0),
        target_symbols=["NVDA"],
        asset_class="stocks",
    )
    # $500 notional / $950 = 0.526 share — fails the whole-lot check.
    results = orch.spec_readiness_gate.validate(spec, backtest_config=_config())
    assert any("qty=" in c and "NVDA" in c for c in _critical(results))


def test_readiness_price_provider_falls_back_on_failure() -> None:
    """Provider returns the static fallback when the data service raises."""
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    orch = StrategyLabOrchestrator()

    def boom(*_a, **_kw):
        raise RuntimeError("network down")

    orch.market_data_service.fetch_ohlcv = boom
    price = orch._readiness_price_provider("AAPL", "stocks")
    assert price == 100.0


def test_custom_market_sample_provider_is_used() -> None:
    spec = _spec(
        sizing=FixedNotionalSizing(notional_usd=500.0),
        target_symbols=["AAPL"],
    )
    # Pretend AAPL is $1000 — $500 notional yields 0.5 share, should fail.
    gate = SpecReadinessGate(market_sample_provider=lambda sym, asset_class: 1000.0)
    results = gate.validate(spec, backtest_config=_config())
    assert any("qty=" in c for c in _critical(results))
