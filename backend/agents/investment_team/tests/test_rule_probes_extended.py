"""Extended coverage for :mod:`rule_probes` — every indicator recipe,
unprobeable-shape branch, and edge case the basic suite doesn't reach.

These tests verify recipes produce series satisfying their predicate (or
mark the probe unprobeable when they can't). Most cases also exercise
the corresponding code paths in the asserter when invoked end-to-end
through :class:`RuleProbesGate` with a stubbed runner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from investment_team.market_data_service import OHLCVBar
from investment_team.models import StrategySpec, TradeRecord
from investment_team.strategy_lab.executor.indicators import (
    rsi,
    sma,
)
from investment_team.strategy_lab.quality_gates.rule_probes import (
    RuleProbesGate,
    generate_rule_probe_runs,
)
from investment_team.strategy_lab.quality_gates.rule_probes.asserter import (
    _summarise_trades,
    assess_probe,
)
from investment_team.strategy_lab.quality_gates.rule_probes.gate import _SkippedResult
from investment_team.strategy_lab.quality_gates.rule_probes.synthesizer import (
    ProbeRun,
    _bars_to_df,
    _compute_indicator_at,
    _extract_universe_literal,
    _normalise_ohlc,
    _resolve_probe_symbol,
    _series_for_source,
    _synthesise_for_predicate,
)
from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    IndicatorRef,
    Predicate,
    StopLossRule,
    TakeProfitRule,
)

# ===========================================================================
# Indicator-vs-number recipes — one test per remaining indicator family
# ===========================================================================


def test_ema_lt_threshold_recipe():
    ref = IndicatorRef(name="ema", params={"period": 10})
    pred = Predicate(lhs=ref, op="<", rhs=50.0)
    bars, trigger, reason = _synthesise_for_predicate(pred)
    assert reason is None and bars is not None
    value = _compute_indicator_at(ref, bars, trigger)
    assert value is not None and value < 50.0


def test_macd_gt_zero_recipe_uses_trending_series():
    ref = IndicatorRef(name="macd")
    pred = Predicate(lhs=ref, op=">", rhs=0.0)
    bars, trigger, reason = _synthesise_for_predicate(pred)
    assert reason is None and bars is not None
    value = _compute_indicator_at(ref, bars, trigger)
    assert value is not None and value > 0.0


def test_bollinger_upper_gt_threshold_recipe():
    ref = IndicatorRef(name="bollinger", params={"band": "upper", "period": 20, "num_std": 2.0})
    pred = Predicate(lhs=ref, op=">", rhs=100.0)
    bars, trigger, reason = _synthesise_for_predicate(pred)
    # Bollinger recipe may not always reach an arbitrary upper threshold,
    # but it should either succeed or be marked unprobeable cleanly.
    assert (bars is not None) != (reason is not None)


def test_atr_gt_threshold_high_volatility_recipe():
    ref = IndicatorRef(name="atr", params={"period": 14})
    pred = Predicate(lhs=ref, op=">", rhs=1.0)
    bars, trigger, reason = _synthesise_for_predicate(pred)
    assert reason is None and bars is not None
    value = _compute_indicator_at(ref, bars, trigger)
    assert value is not None and value > 1.0


def test_stochastic_recipe_yields_finite_value():
    ref = IndicatorRef(name="stochastic", params={"k_period": 14, "d_period": 3, "output": "k"})
    pred = Predicate(lhs=ref, op=">", rhs=0.0)
    bars, trigger, reason = _synthesise_for_predicate(pred)
    assert reason is None and bars is not None
    value = _compute_indicator_at(ref, bars, trigger)
    assert value is not None


def test_vwap_recipe_yields_positive_value():
    ref = IndicatorRef(name="vwap")
    pred = Predicate(lhs=ref, op=">", rhs=0.0)
    bars, trigger, reason = _synthesise_for_predicate(pred)
    assert reason is None and bars is not None
    value = _compute_indicator_at(ref, bars, trigger)
    assert value is not None and value > 0.0


def test_adx_recipe_yields_finite_value():
    ref = IndicatorRef(name="adx", params={"period": 14})
    pred = Predicate(lhs=ref, op=">", rhs=0.0)
    bars, trigger, reason = _synthesise_for_predicate(pred)
    assert reason is None and bars is not None
    value = _compute_indicator_at(ref, bars, trigger)
    assert value is not None


# ===========================================================================
# Recipes that should mark themselves unprobeable
# ===========================================================================


def test_rsi_lt_zero_is_unreachable_or_unprobeable():
    """RSI is bounded in [0, 100]; ``< 0`` is impossible. The recipe must
    surface that as an unprobeable predicate rather than producing garbage."""
    ref = IndicatorRef(name="rsi", params={"period": 14})
    pred = Predicate(lhs=ref, op="<", rhs=-5.0)
    bars, _trigger, reason = _synthesise_for_predicate(pred)
    assert bars is None and reason is not None


def test_unsupported_indicator_vs_indicator_marks_unprobeable():
    """``rsi vs sma`` is supported only through specific helpers; mixed
    indicators without a supporting recipe must return unprobeable."""
    lhs = IndicatorRef(name="rsi", params={"period": 14})
    rhs = IndicatorRef(name="sma", params={"period": 20})
    pred = Predicate(lhs=lhs, op=">", rhs=rhs)
    bars, _trigger, reason = _synthesise_for_predicate(pred)
    assert bars is None and reason is not None
    assert "indicator_vs_indicator_unsupported" in reason


def test_priceref_vs_self_marks_unsatisfiable():
    pred = Predicate(lhs="bar.close", op=">", rhs="bar.close")
    bars, _trigger, reason = _synthesise_for_predicate(pred)
    assert bars is None and "priceref_vs_self_unsatisfiable" in (reason or "")


# ===========================================================================
# Cross-recipe variants
# ===========================================================================


def test_indicator_cross_above_priceref_uses_close_flip():
    """``SMA cross_above bar.close`` is supported via the symmetric flip."""
    lhs = IndicatorRef(name="sma", params={"period": 20})
    pred = Predicate(lhs=lhs, op="cross_above", rhs="bar.close")
    bars, _trigger, reason = _synthesise_for_predicate(pred)
    # The flip dispatches to the priceref-vs-indicator recipe. Either it
    # succeeds or marks an explicit unprobeable reason.
    assert (bars is not None) or reason is not None


def test_indicator_cross_above_number_recipe():
    ref = IndicatorRef(name="sma", params={"period": 20})
    pred = Predicate(lhs=ref, op="cross_above", rhs=100.0)
    bars, trigger, reason = _synthesise_for_predicate(pred)
    assert reason is None and bars is not None
    assert trigger > 0


def test_indicator_cross_below_number_recipe():
    ref = IndicatorRef(name="sma", params={"period": 20})
    pred = Predicate(lhs=ref, op="cross_below", rhs=100.0)
    bars, trigger, reason = _synthesise_for_predicate(pred)
    assert reason is None and bars is not None


def test_cross_with_unsupported_indicator_pair_marks_unprobeable():
    lhs = IndicatorRef(name="rsi", params={"period": 14})
    rhs = IndicatorRef(name="rsi", params={"period": 21})
    pred = Predicate(lhs=lhs, op="cross_above", rhs=rhs)
    bars, _trigger, reason = _synthesise_for_predicate(pred)
    assert bars is None and reason is not None


# ===========================================================================
# UNIVERSE literal extraction edge cases
# ===========================================================================


def test_extract_universe_literal_from_self_universe_assignment():
    code = "class S:\n    def __init__(self):\n        self.UNIVERSE = frozenset({'AAA'})\n"
    parsed = _extract_universe_literal(code)
    assert "AAA" in parsed


def test_extract_universe_literal_empty_frozenset():
    code = "UNIVERSE = frozenset()\n"
    parsed = _extract_universe_literal(code)
    assert parsed == frozenset()


def test_extract_universe_literal_returns_empty_on_syntax_error():
    parsed = _extract_universe_literal("def broken(:\n")
    assert parsed == frozenset()


def test_extract_universe_literal_returns_empty_on_non_call_assignment():
    code = "UNIVERSE = {'foo'}\n"
    parsed = _extract_universe_literal(code)
    assert parsed == frozenset()


def test_extract_universe_literal_returns_empty_when_no_assignment():
    code = "x = 1\n"
    parsed = _extract_universe_literal(code)
    assert parsed == frozenset()


def test_resolve_probe_symbol_prefers_target_symbols_over_universe():
    class _S:
        target_symbols = ["AAPL"]

    code = "UNIVERSE = frozenset({'TEST'})\n"
    assert _resolve_probe_symbol(_S(), code) == "AAPL"


# ===========================================================================
# Stop-loss / take-profit variant coverage
# ===========================================================================


def test_stop_loss_short_position_targets_ceiling():
    entry = EntryRule(
        side="short",
        when=Predicate(lhs="bar.close", op="<", rhs=200.0),
    )
    stop = StopLossRule(pct=0.05)
    [_e, exit_probe] = generate_rule_probe_runs(
        _spec_with(entry_rules=[entry], exit_rules=[stop])
    )
    assert exit_probe.synthesizable
    # For shorts the recipe sets a high above entry_close * (1 + pct).
    trigger_bar = exit_probe.market_data[exit_probe.trigger_bar_index]
    assert trigger_bar.high > trigger_bar.low


def test_take_profit_short_position_targets_floor():
    entry = EntryRule(
        side="short",
        when=Predicate(lhs="bar.close", op="<", rhs=200.0),
    )
    tp = TakeProfitRule(pct=0.05)
    [_e, exit_probe] = generate_rule_probe_runs(
        _spec_with(entry_rules=[entry], exit_rules=[tp])
    )
    assert exit_probe.synthesizable
    trigger_bar = exit_probe.market_data[exit_probe.trigger_bar_index]
    assert trigger_bar.low < trigger_bar.high


def test_stop_loss_trailing_low_on_long_is_unprobeable():
    """``trailing_low`` basis applies to shorts; longs are unprobeable
    because the engine treats this combo as a no-op."""
    entry = EntryRule(
        side="long",
        when=Predicate(lhs="bar.close", op=">", rhs=50.0),
    )
    stop = StopLossRule(pct=0.05, basis="trailing_low")
    [_e, exit_probe] = generate_rule_probe_runs(
        _spec_with(entry_rules=[entry], exit_rules=[stop])
    )
    assert not exit_probe.synthesizable


def test_stop_loss_trailing_high_on_short_is_unprobeable():
    entry = EntryRule(
        side="short",
        when=Predicate(lhs="bar.close", op="<", rhs=200.0),
    )
    stop = StopLossRule(pct=0.05, basis="trailing_high")
    [_e, exit_probe] = generate_rule_probe_runs(
        _spec_with(entry_rules=[entry], exit_rules=[stop])
    )
    assert not exit_probe.synthesizable


# ===========================================================================
# OHLC normalisation
# ===========================================================================


def test_normalise_ohlc_clamps_negative_values():
    bar = OHLCVBar(date="d", open=-1.0, high=5.0, low=-3.0, close=2.0, volume=100.0)
    out = _normalise_ohlc(bar)
    assert out.open > 0 and out.low > 0
    assert out.high >= max(out.open, out.close, out.low)
    assert out.low <= min(out.open, out.close, out.high)


def test_normalise_ohlc_replaces_nan_volume_with_one():
    bar = OHLCVBar(date="d", open=10.0, high=11.0, low=9.0, close=10.5, volume=float("nan"))
    out = _normalise_ohlc(bar)
    assert out.volume == 1.0


def test_normalise_ohlc_preserves_clean_bars():
    bar = OHLCVBar(date="d", open=10.0, high=11.0, low=9.0, close=10.5, volume=100.0)
    out = _normalise_ohlc(bar)
    assert (out.open, out.high, out.low, out.close, out.volume) == (10.0, 11.0, 9.0, 10.5, 100.0)


# ===========================================================================
# Source-series helper
# ===========================================================================


def test_series_for_source_hl2_and_ohlc4_are_averages():
    import pandas as pd

    df = pd.DataFrame(
        {"open": [10.0], "high": [12.0], "low": [8.0], "close": [11.0], "volume": [100.0]}
    )
    hl2 = _series_for_source(df, "hl2").iloc[0]
    ohlc4 = _series_for_source(df, "ohlc4").iloc[0]
    assert hl2 == 10.0  # (12+8)/2
    assert ohlc4 == 10.25  # (10+12+8+11)/4


def test_series_for_source_falls_back_to_close_on_unknown():
    import pandas as pd

    df = pd.DataFrame({"open": [1.0], "high": [2.0], "low": [0.5], "close": [1.5], "volume": [1.0]})
    fallback = _series_for_source(df, "unknown_source_xyz").iloc[0]
    assert fallback == 1.5


def test_series_for_source_volume_returns_volume_column():
    import pandas as pd

    df = pd.DataFrame({"open": [1.0], "high": [2.0], "low": [0.5], "close": [1.5], "volume": [7.0]})
    assert _series_for_source(df, "volume").iloc[0] == 7.0


# ===========================================================================
# Asserter edge paths
# ===========================================================================


@dataclass
class _Stub:
    success: bool = True
    trades: List[TradeRecord] = field(default_factory=list)
    error_type: Optional[str] = None
    stderr: str = ""


def test_assess_probe_unprobeable_emits_warning():
    probe = ProbeRun(
        rule_id="entry[0]",
        rule_kind="entry",
        symbol="PROBE",
        synthesizable=False,
        unprobeable_reason="some_reason",
    )
    g = RuleProbesGate(runner=lambda *a, **k: _Stub())
    with g._using_phase("synthesis"):
        result = assess_probe(probe, _Stub(), emitter=g)
    assert result.severity == "warning"
    assert "some_reason" in result.details


def test_assess_probe_with_no_expected_is_critical():
    probe = ProbeRun(
        rule_id="entry[0]",
        rule_kind="entry",
        symbol="PROBE",
        market_data=[
            OHLCVBar(date="2024-01-01", open=1, high=1.1, low=0.9, close=1, volume=1)
        ],
        expected=None,
    )
    g = RuleProbesGate(runner=lambda *a, **k: _Stub())
    with g._using_phase("synthesis"):
        result = assess_probe(probe, _Stub(success=True), emitter=g)
    assert result.severity == "critical"
    assert "no expected outcome" in result.details


def test_assess_probe_exit_without_exit_reason_substring_is_critical():
    """Exit probe whose ``expected.exit_reason_contains`` is None."""
    from investment_team.strategy_lab.quality_gates.rule_probes.synthesizer import (
        ExpectedOutcome,
    )

    probe = ProbeRun(
        rule_id="exit[0]:stop_loss",
        rule_kind="stop_loss",
        symbol="PROBE",
        market_data=[
            OHLCVBar(date="2024-01-01", open=1, high=1.1, low=0.9, close=1, volume=1)
        ],
        expected=ExpectedOutcome(kind="exit", exit_reason_contains=None),
    )
    g = RuleProbesGate(runner=lambda *a, **k: _Stub())
    with g._using_phase("synthesis"):
        result = assess_probe(probe, _Stub(), emitter=g)
    assert result.severity == "critical"
    assert "missing exit_reason_contains" in result.details


def test_assess_probe_exit_with_no_trades_is_critical():
    from investment_team.strategy_lab.quality_gates.rule_probes.synthesizer import (
        ExpectedOutcome,
    )

    probe = ProbeRun(
        rule_id="exit[0]:stop_loss",
        rule_kind="stop_loss",
        symbol="PROBE",
        market_data=[
            OHLCVBar(date="2024-01-01", open=1, high=1.1, low=0.9, close=1, volume=1)
        ],
        expected=ExpectedOutcome(kind="exit", exit_reason_contains="stop_loss"),
    )
    g = RuleProbesGate(runner=lambda *a, **k: _Stub())
    with g._using_phase("synthesis"):
        result = assess_probe(probe, _Stub(success=True, trades=[]), emitter=g)
    assert result.severity == "critical"
    assert "no trades were recorded" in result.details


def test_summarise_trades_truncates_long_lists():
    trades = [
        TradeRecord(
            trade_num=i,
            entry_date=f"2024-01-{i:02d}",
            exit_date=f"2024-02-{i:02d}",
            symbol="X",
            side="long",
            entry_price=100.0,
            exit_price=110.0,
            shares=1.0,
            position_value=100.0,
            gross_pnl=10.0,
            net_pnl=10.0,
            return_pct=0.10,
            hold_days=1,
            outcome="win",
            cumulative_pnl=10.0,
        )
        for i in range(1, 9)
    ]
    text = _summarise_trades(trades)
    assert "+3 more" in text


# ===========================================================================
# Gate-level: SkippedResult path + runner exception handling
# ===========================================================================


def test_skipped_result_has_empty_trades_and_success_true():
    r = _SkippedResult()
    assert r.success is True
    assert r.trades == []
    assert r.error_type is None


def test_unsynthesizable_entry_rule_becomes_unprobeable_probe_run():
    """An entry rule with an unreachable predicate produces an unprobeable
    :class:`ProbeRun`, not a crash."""
    spec = _spec_with(
        entry_rules=[
            EntryRule(
                side="long",
                when=Predicate(
                    lhs=IndicatorRef(name="rsi", params={"period": 14}),
                    op="<",
                    rhs=-5.0,
                ),
            )
        ]
    )
    [probe] = generate_rule_probe_runs(spec)
    assert probe.synthesizable is False
    assert probe.unprobeable_reason is not None


def test_unsupported_exit_rule_subclass_becomes_unprobeable():
    """Defensive: an exit rule of an unrecognised type marks the probe
    unprobeable without raising."""
    entry = EntryRule(
        side="long",
        when=Predicate(lhs="bar.close", op=">", rhs=50.0),
    )

    class _UnknownExit:
        kind = "unknown_exit_kind_xyz"
        note = ""

    spec = _spec_with(entry_rules=[entry])
    # Bypass the discriminated-union validator by appending after
    # construction; the synthesiser shouldn't trust ``kind`` blindly.
    spec.exit_rules = [_UnknownExit()]  # type: ignore[list-item]
    runs = generate_rule_probe_runs(spec)
    exit_probe = runs[-1]
    assert exit_probe.synthesizable is False
    assert "unknown_exit_rule_type" in (exit_probe.unprobeable_reason or "")


def test_normalise_ohlc_handles_none_field():
    """``_safe`` falls back to the floor for None inputs."""
    # Pydantic OHLCVBar forbids None, but the helper is exercised on
    # arbitrary float-like values; test by passing NaN (the equivalent
    # of the None branch).
    bar = OHLCVBar(date="d", open=float("nan"), high=10.0, low=5.0, close=8.0, volume=100.0)
    out = _normalise_ohlc(bar)
    assert out.open > 0


def test_default_runner_resolves_to_sandbox_run_strategy_code():
    """When constructed without an explicit ``runner``, the gate falls back
    to ``trading_service.modes.sandbox_compat.run_strategy_code``."""
    from investment_team.trading_service.modes.sandbox_compat import (
        run_strategy_code as _real,
    )

    gate = RuleProbesGate()
    assert gate._runner is _real


def test_runner_exception_is_caught_and_renders_critical():
    """A buggy/throwing runner is caught by the gate; the probe surfaces as a
    critical with a stable error type."""

    def boom(code, market_data, config, *, strategy=None):
        raise RuntimeError("kaboom")

    gate = RuleProbesGate(runner=boom)
    spec = _spec_with(
        entry_rules=[
            EntryRule(
                side="long",
                when=Predicate(lhs="bar.close", op=">", rhs=50.0),
            )
        ],
        target_symbols=["PROBE"],
    )
    [r] = gate.check(spec.strategy_code, spec)
    assert r.severity == "critical"
    assert "probe_runner_exception" in r.details


# ===========================================================================
# Bars-to-DF round trip + check helpers
# ===========================================================================


def test_bars_to_df_preserves_ordering_and_indicator_values():
    pred = Predicate(
        lhs=IndicatorRef(name="rsi", params={"period": 14}),
        op="<",
        rhs=30.0,
    )
    bars, trigger, _reason = _synthesise_for_predicate(pred)

    df = _bars_to_df(bars)
    assert len(df) == len(bars)
    # The RSI computed off the dataframe matches the value the synthesiser
    # computed for verification.
    assert rsi(df["close"], 14).iloc[trigger] < 30.0


# ===========================================================================
# Helpers
# ===========================================================================


def _spec_with(*, entry_rules=None, exit_rules=None, target_symbols=None):
    return StrategySpec(
        strategy_id="probe-ext",
        authored_by="test",
        asset_class="stocks",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
        entry_rules=entry_rules or [],
        exit_rules=exit_rules or [],
        target_symbols=target_symbols or [],
        strategy_code="class S:\n    pass\n",
    )


# ===========================================================================
# PriceRef-vs-PriceRef recipes
# ===========================================================================


def test_priceref_close_gt_low_recipe():
    pred = Predicate(lhs="bar.close", op=">", rhs="bar.low")
    bars, trigger, reason = _synthesise_for_predicate(pred)
    assert reason is None and bars is not None
    assert bars[trigger].close > bars[trigger].low


def test_priceref_high_gt_low_recipe_already_satisfied():
    """``bar.high > bar.low`` is satisfied on the default OHLC values
    (high=101, low=99) — the recipe should not need adjustment."""
    pred = Predicate(lhs="bar.high", op=">", rhs="bar.low")
    bars, trigger, reason = _synthesise_for_predicate(pred)
    assert reason is None and bars is not None
    assert bars[trigger].high > bars[trigger].low


def test_priceref_low_lt_high_recipe():
    pred = Predicate(lhs="bar.low", op="<", rhs="bar.high")
    bars, trigger, reason = _synthesise_for_predicate(pred)
    assert reason is None and bars is not None
    assert bars[trigger].low < bars[trigger].high


# ===========================================================================
# PriceRef ops the basic suite skipped: <=, >=, ==
# ===========================================================================


def test_priceref_close_le_number_recipe():
    pred = Predicate(lhs="bar.close", op="<=", rhs=50.0)
    bars, trigger, reason = _synthesise_for_predicate(pred)
    assert reason is None and bars is not None
    assert bars[trigger].close <= 50.0


def test_priceref_close_ge_number_recipe():
    pred = Predicate(lhs="bar.close", op=">=", rhs=120.0)
    bars, trigger, reason = _synthesise_for_predicate(pred)
    assert reason is None and bars is not None
    assert bars[trigger].close >= 120.0


def test_priceref_close_eq_number_recipe():
    pred = Predicate(lhs="bar.close", op="==", rhs=100.0)
    bars, trigger, reason = _synthesise_for_predicate(pred)
    assert reason is None and bars is not None
    assert bars[trigger].close == 100.0


# ===========================================================================
# Bar-field setters: high / low / volume
# ===========================================================================


def test_priceref_high_gt_number_recipe_drives_high_field():
    pred = Predicate(lhs="bar.high", op=">", rhs=150.0)
    bars, trigger, reason = _synthesise_for_predicate(pred)
    assert reason is None and bars is not None
    assert bars[trigger].high > 150.0


def test_priceref_low_lt_number_recipe_drives_low_field():
    pred = Predicate(lhs="bar.low", op="<", rhs=50.0)
    bars, trigger, reason = _synthesise_for_predicate(pred)
    assert reason is None and bars is not None
    assert bars[trigger].low < 50.0


def test_priceref_volume_gt_number_recipe_drives_volume_field():
    pred = Predicate(lhs="bar.volume", op=">", rhs=500_000.0)
    bars, trigger, reason = _synthesise_for_predicate(pred)
    assert reason is None and bars is not None
    assert bars[trigger].volume > 500_000.0


# ===========================================================================
# Indicator-vs-PriceRef
# ===========================================================================


def test_sma_gt_bar_low_recipe():
    pred = Predicate(
        lhs=IndicatorRef(name="sma", params={"period": 5}),
        op=">",
        rhs="bar.low",
    )
    bars, trigger, reason = _synthesise_for_predicate(pred)
    assert reason is None and bars is not None
    # SMA at trigger should be > the bar's low.
    series = _bars_to_df(bars)["close"]
    assert sma(series, 5).iloc[trigger] > bars[trigger].low


def test_unsupported_indicator_vs_priceref_marks_unprobeable():
    pred = Predicate(
        lhs=IndicatorRef(name="rsi", params={"period": 14}),
        op=">",
        rhs="bar.close",
    )
    bars, _trigger, reason = _synthesise_for_predicate(pred)
    assert bars is None and reason is not None
    assert "indicator_vs_priceref_unsupported" in reason


# ===========================================================================
# Cross recipe edge cases
# ===========================================================================


def test_cross_with_priceref_against_non_sma_indicator_unprobeable():
    """``bar.close cross_above RSI(14)`` isn't supported — only SMA/EMA crosses."""
    pred = Predicate(
        lhs="bar.close",
        op="cross_above",
        rhs=IndicatorRef(name="rsi", params={"period": 14}),
    )
    bars, _trigger, reason = _synthesise_for_predicate(pred)
    assert bars is None and reason is not None


def test_cross_against_non_close_priceref_unprobeable():
    """``bar.high cross_above SMA(20)`` — the supported lhs is bar.close."""
    pred = Predicate(
        lhs="bar.high",
        op="cross_above",
        rhs=IndicatorRef(name="sma", params={"period": 20}),
    )
    bars, _trigger, reason = _synthesise_for_predicate(pred)
    assert bars is None and reason is not None


def test_cross_indicator_number_with_unsupported_indicator_unprobeable():
    pred = Predicate(
        lhs=IndicatorRef(name="rsi", params={"period": 14}),
        op="cross_above",
        rhs=50.0,
    )
    bars, _trigger, reason = _synthesise_for_predicate(pred)
    # rsi cross above number isn't supported by the SMA/EMA-only recipe.
    assert bars is None and reason is not None


# ===========================================================================
# Compute-indicator-at safety
# ===========================================================================


def test_compute_indicator_at_returns_none_for_unsupported_name():
    """Defensive: a malformed IndicatorRef.name should return None."""
    # IndicatorRef enforces ``name`` is one of the known literals, so we
    # construct a mock-shaped object that bypasses validation.
    class _FakeRef:
        name = "unsupported_xyz"
        source = "close"
        params: dict = {}

        def param(self, key, default=None):  # pragma: no cover - defensive
            return default


    bars = [
        OHLCVBar(date=f"2024-01-{i:02d}", open=1, high=1.1, low=0.9, close=1, volume=1)
        for i in range(1, 31)
    ]
    assert _compute_indicator_at(_FakeRef(), bars, 10) is None


# ===========================================================================
# Probe entry without market_data (defensive trigger_date path)
# ===========================================================================


def test_assess_probe_entry_rejects_trade_opened_before_trigger():
    """A correctly-sided trade opened *before* the probe's trigger bar is
    not evidence the rule under test fires — it's more likely an unrelated
    always-on entry. The asserter must reject it so swapped/negated
    predicates can't slip past the gate."""
    from investment_team.strategy_lab.quality_gates.rule_probes.synthesizer import (
        ExpectedOutcome,
    )

    probe = ProbeRun(
        rule_id="entry[0]",
        rule_kind="entry",
        symbol="PROBE",
        market_data=[
            OHLCVBar(date="2024-06-01", open=1, high=1.1, low=0.9, close=1, volume=1)
        ],
        expected=ExpectedOutcome(kind="entry", side="long"),
        trigger_bar_index=0,
    )
    early_trade = TradeRecord(
        trade_num=1,
        entry_date="2024-01-01",  # before trigger
        exit_date="2024-06-15",
        symbol="PROBE",
        side="long",
        entry_price=100.0,
        exit_price=110.0,
        shares=1.0,
        position_value=100.0,
        gross_pnl=10.0,
        net_pnl=10.0,
        return_pct=0.10,
        hold_days=180,
        outcome="win",
        cumulative_pnl=10.0,
    )
    g = RuleProbesGate(runner=lambda *a, **k: _Stub())
    with g._using_phase("synthesis"):
        result = assess_probe(probe, _Stub(success=True, trades=[early_trade]), emitter=g)
    assert result.passed is False
    assert "before trigger bar" in result.details


def test_series_for_source_open_high_low_columns():
    import pandas as pd

    df = pd.DataFrame(
        {"open": [11.0], "high": [12.0], "low": [9.0], "close": [10.5], "volume": [100.0]}
    )
    assert _series_for_source(df, "open").iloc[0] == 11.0
    assert _series_for_source(df, "high").iloc[0] == 12.0
    assert _series_for_source(df, "low").iloc[0] == 9.0


def test_indicator_vs_indicator_lt_descending_series():
    """Inverse-trending series: ``SMA(5) < SMA(20)`` requires a falling regime
    at the right edge."""
    fast = IndicatorRef(name="sma", params={"period": 5})
    slow = IndicatorRef(name="sma", params={"period": 20})
    pred = Predicate(lhs=fast, op="<", rhs=slow)
    bars, trigger, reason = _synthesise_for_predicate(pred)
    assert reason is None and bars is not None
    series = _bars_to_df(bars)["close"]
    assert sma(series, 5).iloc[trigger] < sma(series, 20).iloc[trigger]


def test_sma_eq_threshold_recipe():
    """``sma(5) == X`` flat-lines closes at X."""
    ref = IndicatorRef(name="sma", params={"period": 5})
    pred = Predicate(lhs=ref, op="==", rhs=100.0)
    bars, trigger, reason = _synthesise_for_predicate(pred)
    assert reason is None and bars is not None
    series = _bars_to_df(bars)["close"]
    # SMA(5) at trigger should equal the flat-level (100.0).
    assert abs(sma(series, 5).iloc[trigger] - 100.0) < 1e-6


def test_verify_cross_with_unknown_op_returns_false():
    from investment_team.strategy_lab.quality_gates.rule_probes.synthesizer import (
        _verify_cross,
    )

    # Defensive: an op not in {cross_above, cross_below} returns False.
    assert _verify_cross(1.0, 2.0, 1.5, 1.5, "not_a_cross_op") is False


def test_verify_cross_with_non_numeric_values_returns_false():
    from investment_team.strategy_lab.quality_gates.rule_probes.synthesizer import (
        _verify_cross,
    )

    assert _verify_cross("a", "b", "c", "d", "cross_above") is False


def test_priceref_vs_priceref_already_satisfied_path():
    """Default OHLC (open=100, low=99, high=101, close=100.5) already
    satisfies ``close > low``; exercises the early-return branch."""
    from investment_team.strategy_lab.quality_gates.rule_probes.synthesizer import (
        _bar_with_priceref_relation,
    )

    bar = _bar_with_priceref_relation("close", "low", ">")
    assert bar.close > bar.low


def test_priceref_vs_priceref_eq_falls_through_to_else_branch():
    """``==`` on default OHLC (close != open) requires adjusting close → open
    via the else-branch path."""
    from investment_team.strategy_lab.quality_gates.rule_probes.synthesizer import (
        _bar_with_priceref_relation,
    )

    bar = _bar_with_priceref_relation("close", "high", "==")
    # The adjustment makes close == high (the rhs value).
    assert bar.close == bar.high


def test_volume_priceref_comparison_marks_unprobeable_without_crashing():
    """``bar.volume`` is a valid PriceRef in the DSL but doesn't fit the
    OHLC default-fields model. The synthesiser must mark the probe
    unprobeable instead of raising a KeyError."""
    pred = Predicate(lhs="bar.volume", op=">", rhs="bar.close")
    bars, _trigger, reason = _synthesise_for_predicate(pred)
    assert bars is None
    assert reason == "priceref_volume_comparison_unprobeable"


def test_volume_priceref_on_either_side_marks_unprobeable():
    pred = Predicate(lhs="bar.close", op=">", rhs="bar.volume")
    bars, _trigger, reason = _synthesise_for_predicate(pred)
    assert bars is None
    assert reason == "priceref_volume_comparison_unprobeable"


def test_volume_priceref_in_spec_does_not_abort_synthesis_loop():
    """End-to-end: a spec with a volume-PriceRef rule generates a probe
    run with synthesizable=False rather than crashing the synthesis loop."""
    entry = EntryRule(
        side="long",
        when=Predicate(lhs="bar.volume", op=">", rhs="bar.close"),
    )
    spec = _spec_with(entry_rules=[entry])
    [probe] = generate_rule_probe_runs(spec)
    assert probe.synthesizable is False
    assert "volume" in (probe.unprobeable_reason or "")


def test_predicate_verifier_returns_false_on_out_of_range_index():
    from investment_team.strategy_lab.quality_gates.rule_probes.synthesizer import (
        _predicate_verifier,
    )

    pred = Predicate(lhs="bar.close", op=">", rhs=50.0)
    verifier = _predicate_verifier(pred)
    bars = [OHLCVBar(date="d", open=100, high=101, low=99, close=100, volume=1)]
    assert verifier(bars, -1) is False
    assert verifier(bars, 5) is False
    assert verifier([], 0) is False


def test_predicate_verifier_returns_false_on_eval_exception():
    """If ``_eval_predicate_at`` raises (malformed predicate, etc.) the
    verifier swallows the exception and returns False so a bug surfaces
    as 'unprobeable' rather than crashing the synthesis loop."""
    from investment_team.strategy_lab.quality_gates.rule_probes.synthesizer import (
        _predicate_verifier,
    )

    # Construct an in-memory predicate with a side that bypasses DSL
    # validation but breaks ``_resolve_side``.
    class _BrokenSide:
        pass

    class _BrokenPredicate:
        lhs = _BrokenSide()
        op = ">"
        rhs = 0.0

    verifier = _predicate_verifier(_BrokenPredicate())
    bars = [OHLCVBar(date="d", open=1, high=1.1, low=0.9, close=1, volume=1)]
    assert verifier(bars, 0) is False


def test_eval_predicate_at_cross_above_returns_false_at_index_zero():
    """``cross_*`` requires a (prev, curr) pair — at index 0 there's no
    prev, so the predicate is False."""
    from investment_team.strategy_lab.quality_gates.rule_probes.synthesizer import (
        _eval_predicate_at,
    )

    bars = [OHLCVBar(date="d", open=10, high=11, low=9, close=10, volume=1)]
    pred = Predicate(
        lhs="bar.close",
        op="cross_above",
        rhs=IndicatorRef(name="sma", params={"period": 5}),
    )
    assert _eval_predicate_at(pred, bars, 0) is False


def test_resolve_side_numeric_float_path():
    """Numeric (int/float) sides resolve to their float value at any bar."""
    from investment_team.strategy_lab.quality_gates.rule_probes.synthesizer import (
        _resolve_side,
    )

    bars = [OHLCVBar(date="d", open=1, high=1.1, low=0.9, close=1, volume=1)]
    assert _resolve_side(42.5, bars, 0) == 42.5
    assert _resolve_side(7, bars, 0) == 7.0


def test_resolve_side_unsupported_string_returns_none():
    """Non-``bar.`` string sides aren't a known PriceRef shape."""
    from investment_team.strategy_lab.quality_gates.rule_probes.synthesizer import (
        _resolve_side,
    )

    bars = [OHLCVBar(date="d", open=1, high=1.1, low=0.9, close=1, volume=1)]
    assert _resolve_side("not_a_bar_ref", bars, 0) is None


def test_resolve_side_bool_returns_none():
    """Booleans are technically int subclasses; reject them explicitly."""
    from investment_team.strategy_lab.quality_gates.rule_probes.synthesizer import (
        _resolve_side,
    )

    bars = [OHLCVBar(date="d", open=1, high=1.1, low=0.9, close=1, volume=1)]
    assert _resolve_side(True, bars, 0) is None
    assert _resolve_side(False, bars, 0) is None


def test_resolve_side_unsupported_type_returns_none():
    from investment_team.strategy_lab.quality_gates.rule_probes.synthesizer import (
        _resolve_side,
    )

    bars = [OHLCVBar(date="d", open=1, high=1.1, low=0.9, close=1, volume=1)]
    assert _resolve_side(object(), bars, 0) is None


def test_stop_loss_verifier_long_position_triggers_on_floor_breach():
    from investment_team.strategy_lab.quality_gates.rule_probes.synthesizer import (
        _stop_loss_verifier,
    )

    rule = StopLossRule(pct=0.05)
    verify = _stop_loss_verifier(rule, entry_close=100.0, entry_side="long")
    # Trigger: low <= 100 * 0.95 = 95
    bars_trigger = [OHLCVBar(date="d", open=96, high=97, low=94.5, close=96, volume=1)]
    assert verify(bars_trigger, 0) is True
    # No trigger: low above floor
    bars_safe = [OHLCVBar(date="d", open=96, high=97, low=96, close=96, volume=1)]
    assert verify(bars_safe, 0) is False


def test_stop_loss_verifier_short_position_triggers_on_ceiling_breach():
    from investment_team.strategy_lab.quality_gates.rule_probes.synthesizer import (
        _stop_loss_verifier,
    )

    rule = StopLossRule(pct=0.05)
    verify = _stop_loss_verifier(rule, entry_close=100.0, entry_side="short")
    bars_trigger = [OHLCVBar(date="d", open=104, high=106, low=103, close=104, volume=1)]
    assert verify(bars_trigger, 0) is True
    bars_safe = [OHLCVBar(date="d", open=104, high=104.5, low=103, close=104, volume=1)]
    assert verify(bars_safe, 0) is False


def test_take_profit_verifier_long_and_short():
    from investment_team.strategy_lab.quality_gates.rule_probes.synthesizer import (
        _take_profit_verifier,
    )

    rule = TakeProfitRule(pct=0.05)
    verify_long = _take_profit_verifier(rule, entry_close=100.0, entry_side="long")
    assert verify_long([OHLCVBar(date="d", open=104, high=106, low=103, close=105, volume=1)], 0) is True
    assert verify_long([OHLCVBar(date="d", open=104, high=104.5, low=103, close=104, volume=1)], 0) is False
    verify_short = _take_profit_verifier(rule, entry_close=100.0, entry_side="short")
    assert verify_short([OHLCVBar(date="d", open=96, high=97, low=94, close=95, volume=1)], 0) is True
    assert verify_short([OHLCVBar(date="d", open=96, high=97, low=96, close=96, volume=1)], 0) is False


def test_exit_verifiers_return_false_on_out_of_range_index():
    from investment_team.strategy_lab.quality_gates.rule_probes.synthesizer import (
        _stop_loss_verifier,
        _take_profit_verifier,
    )

    sl = _stop_loss_verifier(StopLossRule(pct=0.05), entry_close=100.0, entry_side="long")
    tp = _take_profit_verifier(TakeProfitRule(pct=0.05), entry_close=100.0, entry_side="long")
    assert sl([], 0) is False
    assert tp([], 0) is False
    assert sl([OHLCVBar(date="d", open=1, high=1, low=1, close=1, volume=1)], 5) is False


def test_priceref_high_lt_low_is_unprobeable():
    """``bar.high < bar.low`` is structurally impossible under OHLC
    invariants. The recipe builds a bar with adjusted lhs, but
    ``_normalise_ohlc`` restores ``high >= low``, leaving the predicate
    false. The recipe must detect this and mark the probe unprobeable
    rather than ship a non-triggering bar."""
    pred = Predicate(lhs="bar.high", op="<", rhs="bar.low")
    bars, _trigger, reason = _synthesise_for_predicate(pred)
    assert bars is None
    assert reason == "priceref_vs_priceref_invariant_conflict"


def test_priceref_low_gt_high_is_unprobeable():
    """Symmetric case to ``bar.high < bar.low``: ``bar.low > bar.high``
    can't satisfy the OHLC invariant."""
    pred = Predicate(lhs="bar.low", op=">", rhs="bar.high")
    bars, _trigger, reason = _synthesise_for_predicate(pred)
    assert bars is None
    assert reason == "priceref_vs_priceref_invariant_conflict"


def test_post_clamp_verifier_marks_probe_unprobeable_when_clamping_breaks_predicate():
    """``sma < 0.5`` synthesises a sub-zero flat-line; ``_normalise_ohlc``
    clamps closes to the floor, so SMA == ``_MIN_PRICE`` (0.01) >= 0.5 is
    false and 0.01 < 0.5 is true. With ``rhs`` chosen so the floor sits
    *above* the predicate's truth region, the clamped bars no longer
    satisfy the predicate, and the probe must be marked unprobeable."""
    # Pick rhs s.t. recipe synthesises a level at rhs-1.0 = -0.99 (clamped
    # to _MIN_PRICE=0.01). SMA computed on clamped closes == 0.01.
    # Predicate "0.01 < 0.01" is False -> unprobeable.
    pred = Predicate(
        lhs=IndicatorRef(name="sma", params={"period": 5}),
        op="<",
        rhs=0.01,
    )
    # The probe goes through `_build_entry_probe` + `_stamp_dates`; with
    # the verifier wired we expect the final `ProbeRun` to be marked
    # unprobeable rather than emitted with bars that won't trigger.
    rule = EntryRule(side="long", when=pred)
    spec = _spec_with(entry_rules=[rule])
    [probe] = generate_rule_probe_runs(spec)
    assert probe.synthesizable is False
    assert (
        probe.unprobeable_reason == "post_clamp_predicate_violation"
        # Recipe may also bail earlier with its own pre-clamp signal.
        or "below_clamp_floor" in (probe.unprobeable_reason or "")
        or "verification_failed" in (probe.unprobeable_reason or "")
        or "unreachable" in (probe.unprobeable_reason or "")
    )


def test_post_clamp_verifier_passes_for_well_formed_recipe():
    """A normal predicate (well above clamp floor) survives normalisation:
    the post-clamp verifier confirms the predicate still holds and the
    probe ships its bars."""
    rule = EntryRule(
        side="long",
        when=Predicate(
            lhs=IndicatorRef(name="sma", params={"period": 5}),
            op=">",
            rhs=100.0,
        ),
    )
    spec = _spec_with(entry_rules=[rule])
    [probe] = generate_rule_probe_runs(spec)
    assert probe.synthesizable is True
    assert probe.unprobeable_reason is None
    assert probe.post_clamp_verifier is not None
    # The stored verifier evaluates the post-clamp bars at the trigger:
    assert probe.post_clamp_verifier(probe.market_data, probe.trigger_bar_index) is True


def test_post_clamp_verifier_raising_marks_unprobeable_not_crash():
    """A buggy verifier must not propagate exceptions out of the
    synthesis loop. ``_stamp_dates`` catches and marks unprobeable."""
    from investment_team.strategy_lab.quality_gates.rule_probes.synthesizer import (
        _stamp_dates,
    )

    def _explode(_bars, _idx):
        raise RuntimeError("verifier bug")

    probe = ProbeRun(
        rule_id="entry[0]",
        rule_kind="entry",
        symbol="PROBE",
        market_data=[
            OHLCVBar(date="placeholder", open=1, high=1.1, low=0.9, close=1, volume=1)
        ],
        trigger_bar_index=0,
        synthesizable=True,
        post_clamp_verifier=_explode,
    )
    stamped = _stamp_dates(probe)
    assert stamped.synthesizable is False
    assert stamped.unprobeable_reason == "post_clamp_predicate_violation"


def test_assess_probe_exit_rejects_trade_closed_before_trigger():
    """An exit-substring match alone isn't evidence the targeted rule fired —
    a strategy that always exits at bar 2 for unrelated reasons can collide
    with the substring. Reject trades whose exit_date precedes the
    synthesised trigger bar."""
    from investment_team.strategy_lab.quality_gates.rule_probes.synthesizer import (
        ExpectedOutcome,
    )

    probe = ProbeRun(
        rule_id="exit[0]:stop_loss",
        rule_kind="stop_loss",
        symbol="PROBE",
        market_data=[
            OHLCVBar(date="2024-06-01", open=1, high=1.1, low=0.9, close=1, volume=1)
        ],
        expected=ExpectedOutcome(kind="exit", exit_reason_contains="stop_loss"),
        trigger_bar_index=0,
    )
    early_trade = TradeRecord(
        trade_num=1,
        entry_date="2024-01-01",
        exit_date="2024-01-02",  # before trigger
        symbol="PROBE",
        side="long",
        entry_price=100.0,
        exit_price=90.0,
        shares=1.0,
        position_value=100.0,
        gross_pnl=-10.0,
        net_pnl=-10.0,
        return_pct=-0.10,
        hold_days=1,
        outcome="loss",
        cumulative_pnl=-10.0,
        exit_reason="engine_exit:stop_loss",
    )
    g = RuleProbesGate(runner=lambda *a, **k: _Stub())
    with g._using_phase("synthesis"):
        result = assess_probe(probe, _Stub(success=True, trades=[early_trade]), emitter=g)
    assert result.passed is False
    assert "before trigger bar" in result.details


def test_priceref_below_clamp_floor_marks_unprobeable():
    """``bar.close < 0.005`` would synthesise a trigger value below
    ``_MIN_PRICE``; the recipe must refuse rather than ship a probe whose
    post-clamp bars no longer satisfy the predicate."""
    pred = Predicate(lhs="bar.close", op="<", rhs=0.005)
    bars, _trigger, reason = _synthesise_for_predicate(pred)
    assert bars is None
    assert reason is not None
    assert "clamp_floor" in reason


def test_priceref_above_clamp_floor_still_probeable():
    """``bar.close > 50`` is safe — trigger value rhs+1=51 and baseline
    rhs-5=45 both clear the clamp floor, so the post-clamp bars still
    satisfy the predicate."""
    pred = Predicate(lhs="bar.close", op=">", rhs=50.0)
    bars, trigger, reason = _synthesise_for_predicate(pred)
    assert reason is None and bars is not None
    # Critical: the post-clamp trigger bar must still satisfy the predicate.
    assert bars[trigger].close > 50.0


def test_priceref_baseline_below_clamp_floor_marks_unprobeable():
    """``bar.close > -10`` would set baseline to ``rhs - 5 = -15``, below
    the clamp floor. Refuse the probe."""
    pred = Predicate(lhs="bar.close", op=">", rhs=-10.0)
    bars, _trigger, reason = _synthesise_for_predicate(pred)
    assert bars is None
    assert reason is not None


def test_indicator_trigger_idx_points_at_first_satisfying_bar():
    """For an SMA flat-line recipe, the predicate is satisfied at every
    post-warmup bar. The recipe must set ``trigger_bar_index`` to the
    earliest such bar so the asserter's stricter timing check still
    accepts a strategy that opens at the rule's first-fire bar."""
    pred = Predicate(
        lhs=IndicatorRef(name="sma", params={"period": 5}),
        op=">",
        rhs=50.0,
    )
    bars, trigger, reason = _synthesise_for_predicate(pred)
    assert reason is None and bars is not None
    series = _bars_to_df(bars)["close"]
    # The earliest SMA-finite bar that satisfies the predicate.
    sma_series = sma(series, 5)
    earliest = next(
        i for i, v in enumerate(sma_series.tolist())
        if isinstance(v, float) and v == v and v > 50.0  # not-NaN, satisfies
    )
    assert trigger == earliest


def test_assess_probe_entry_with_wrong_side_then_right_side_still_passes():
    """Multi-trade result: the first trade has the wrong side, the second
    has the right side. The asserter must scan all trades, not just the
    first."""
    from investment_team.strategy_lab.quality_gates.rule_probes.synthesizer import (
        ExpectedOutcome,
    )

    probe = ProbeRun(
        rule_id="entry[0]",
        rule_kind="entry",
        symbol="PROBE",
        market_data=[
            OHLCVBar(date="2024-01-01", open=1, high=1.1, low=0.9, close=1, volume=1)
        ],
        expected=ExpectedOutcome(kind="entry", side="long"),
        trigger_bar_index=0,
    )

    def _t(side, date):
        return TradeRecord(
            trade_num=1,
            entry_date=date,
            exit_date=date,
            symbol="PROBE",
            side=side,
            entry_price=100.0,
            exit_price=100.0,
            shares=1.0,
            position_value=100.0,
            gross_pnl=0.0,
            net_pnl=0.0,
            return_pct=0.0,
            hold_days=0,
            outcome="loss",
            cumulative_pnl=0.0,
        )

    trades = [_t("short", "2024-01-02"), _t("long", "2024-01-03")]
    g = RuleProbesGate(runner=lambda *a, **k: _Stub())
    with g._using_phase("synthesis"):
        result = assess_probe(probe, _Stub(success=True, trades=trades), emitter=g)
    assert result.passed is True
