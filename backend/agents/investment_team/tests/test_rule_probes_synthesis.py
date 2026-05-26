"""Per-recipe correctness tests for the bar-synthesiser.

These tests do not invoke the sandbox — they only check that each
recipe produces a series in which the target predicate evaluates True
on the recorded trigger bar, using the same indicator helpers the
compiler emits calls to.
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from investment_team.strategy_lab.executor.indicators import ema, rsi, sma
from investment_team.strategy_lab.quality_gates.rule_probes.synthesizer import (
    ProbeRun,
    _bars_to_df,
    _compare,
    _compute_indicator_at,
    _synthesise_for_predicate,
    _verify_cross,
    generate_rule_probe_runs,
)
from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    IndicatorRef,
    Predicate,
    SignalExitRule,
    StopLossRule,
    TakeProfitRule,
)


def _spec_with(entry_rules=None, exit_rules=None, target_symbols=None):
    """Build the minimal spec shape ``generate_rule_probe_runs`` needs."""

    class _MiniSpec:
        pass

    s = _MiniSpec()
    s.entry_rules = entry_rules or []
    s.exit_rules = exit_rules or []
    s.target_symbols = target_symbols or []
    return s


# ---------------------------------------------------------------------------
# Trivial: PriceRef vs number
# ---------------------------------------------------------------------------


def test_priceref_close_lt_number_satisfies_at_trigger():
    pred = Predicate(lhs="bar.close", op="<", rhs=50.0)
    bars, trigger, reason = _synthesise_for_predicate(pred)
    assert reason is None and bars is not None
    assert bars[trigger].close < 50.0


def test_priceref_close_gt_number_satisfies_at_trigger():
    pred = Predicate(lhs="bar.close", op=">", rhs=120.0)
    bars, trigger, reason = _synthesise_for_predicate(pred)
    assert reason is None and bars is not None
    assert bars[trigger].close > 120.0


# ---------------------------------------------------------------------------
# RSI threshold recipes
# ---------------------------------------------------------------------------


def test_rsi_lt_recipe_satisfies_predicate():
    ref = IndicatorRef(name="rsi", params={"period": 14})
    pred = Predicate(lhs=ref, op="<", rhs=30.0)
    bars, trigger, reason = _synthesise_for_predicate(pred)
    assert reason is None and bars is not None
    series = pd.Series([b.close for b in bars])
    value = rsi(series, period=14).iloc[trigger]
    assert math.isfinite(value)
    assert value < 30.0


def test_rsi_gt_recipe_satisfies_predicate():
    ref = IndicatorRef(name="rsi", params={"period": 14})
    pred = Predicate(lhs=ref, op=">", rhs=70.0)
    bars, trigger, reason = _synthesise_for_predicate(pred)
    assert reason is None and bars is not None
    series = pd.Series([b.close for b in bars])
    value = rsi(series, period=14).iloc[trigger]
    assert math.isfinite(value)
    assert value > 70.0


# ---------------------------------------------------------------------------
# SMA threshold recipe (sanity — flat line at level)
# ---------------------------------------------------------------------------


def test_sma_gt_recipe_satisfies_predicate():
    ref = IndicatorRef(name="sma", params={"period": 20})
    pred = Predicate(lhs=ref, op=">", rhs=100.0)
    bars, trigger, reason = _synthesise_for_predicate(pred)
    assert reason is None and bars is not None
    series = pd.Series([b.close for b in bars])
    value = sma(series, 20).iloc[trigger]
    assert math.isfinite(value) and value > 100.0


# ---------------------------------------------------------------------------
# Cross_above / cross_below recipes — must satisfy the (prev, curr) pair
# semantics the compiler's predicate emitter uses.
# ---------------------------------------------------------------------------


def test_close_cross_above_sma_uses_prev_curr_pair_semantics():
    ref = IndicatorRef(name="sma", params={"period": 20})
    pred = Predicate(lhs="bar.close", op="cross_above", rhs=ref)
    bars, trigger, reason = _synthesise_for_predicate(pred)
    assert reason is None and bars is not None
    series = pd.Series([b.close for b in bars])
    ma = sma(series, 20)
    assert _verify_cross(
        series.iloc[trigger - 1],
        series.iloc[trigger],
        ma.iloc[trigger - 1],
        ma.iloc[trigger],
        "cross_above",
    )


def test_close_cross_below_sma_uses_prev_curr_pair_semantics():
    ref = IndicatorRef(name="sma", params={"period": 20})
    pred = Predicate(lhs="bar.close", op="cross_below", rhs=ref)
    bars, trigger, reason = _synthesise_for_predicate(pred)
    assert reason is None and bars is not None
    series = pd.Series([b.close for b in bars])
    ma = sma(series, 20)
    assert _verify_cross(
        series.iloc[trigger - 1],
        series.iloc[trigger],
        ma.iloc[trigger - 1],
        ma.iloc[trigger],
        "cross_below",
    )


def test_close_cross_above_ema_uses_prev_curr_pair_semantics():
    ref = IndicatorRef(name="ema", params={"period": 10})
    pred = Predicate(lhs="bar.close", op="cross_above", rhs=ref)
    bars, trigger, reason = _synthesise_for_predicate(pred)
    assert reason is None and bars is not None
    series = pd.Series([b.close for b in bars])
    ma = ema(series, 10)
    assert _verify_cross(
        series.iloc[trigger - 1],
        series.iloc[trigger],
        ma.iloc[trigger - 1],
        ma.iloc[trigger],
        "cross_above",
    )


# ---------------------------------------------------------------------------
# Indicator-vs-indicator: SMA-fast crosses SMA-slow at the regime change
# ---------------------------------------------------------------------------


def test_sma_fast_gt_sma_slow_two_regime():
    fast = IndicatorRef(name="sma", params={"period": 5})
    slow = IndicatorRef(name="sma", params={"period": 20})
    pred = Predicate(lhs=fast, op=">", rhs=slow)
    bars, trigger, reason = _synthesise_for_predicate(pred)
    assert reason is None and bars is not None
    series = pd.Series([b.close for b in bars])
    assert sma(series, 5).iloc[trigger] > sma(series, 20).iloc[trigger]


def test_sma_fast_cross_above_sma_slow():
    fast = IndicatorRef(name="sma", params={"period": 5})
    slow = IndicatorRef(name="sma", params={"period": 30})
    pred = Predicate(lhs=fast, op="cross_above", rhs=slow)
    bars, trigger, reason = _synthesise_for_predicate(pred)
    assert reason is None and bars is not None
    series = pd.Series([b.close for b in bars])
    fast_s = sma(series, 5)
    slow_s = sma(series, 30)
    assert _verify_cross(
        fast_s.iloc[trigger - 1],
        fast_s.iloc[trigger],
        slow_s.iloc[trigger - 1],
        slow_s.iloc[trigger],
        "cross_above",
    )


# ---------------------------------------------------------------------------
# Stop-loss and take-profit tail recipes
# ---------------------------------------------------------------------------


def test_stop_loss_long_tail_last_bar_violates_floor():
    entry_rule = EntryRule(
        side="long",
        when=Predicate(lhs="bar.close", op=">", rhs=50.0),
    )
    stop = StopLossRule(pct=0.03)
    runs = generate_rule_probe_runs(
        _spec_with(entry_rules=[entry_rule], exit_rules=[stop])
    )
    [_entry, exit_probe] = runs
    assert exit_probe.synthesizable
    entry_bars = exit_probe.market_data[: exit_probe.trigger_bar_index]
    trigger_bar = exit_probe.market_data[exit_probe.trigger_bar_index]
    # The opening trade fires at the entry recipe's trigger bar; use that
    # bar's close as the reference entry price.
    entry_close = entry_bars[-1].close if entry_bars else trigger_bar.close
    assert trigger_bar.low <= entry_close * (1.0 - stop.pct)


def test_take_profit_long_tail_last_bar_breaches_target():
    entry_rule = EntryRule(
        side="long",
        when=Predicate(lhs="bar.close", op=">", rhs=50.0),
    )
    tp = TakeProfitRule(pct=0.05)
    runs = generate_rule_probe_runs(
        _spec_with(entry_rules=[entry_rule], exit_rules=[tp])
    )
    [_entry, exit_probe] = runs
    assert exit_probe.synthesizable
    entry_bars = exit_probe.market_data[: exit_probe.trigger_bar_index]
    trigger_bar = exit_probe.market_data[exit_probe.trigger_bar_index]
    entry_close = entry_bars[-1].close if entry_bars else trigger_bar.close
    assert trigger_bar.high >= entry_close * (1.0 + tp.pct)


def test_signal_exit_tail_satisfies_predicate_after_entry_prefix():
    entry_rule = EntryRule(
        side="long",
        when=Predicate(lhs="bar.close", op=">", rhs=50.0),
    )
    signal = SignalExitRule(when=Predicate(lhs="bar.close", op="<", rhs=10.0))
    runs = generate_rule_probe_runs(
        _spec_with(entry_rules=[entry_rule], exit_rules=[signal])
    )
    [_entry, exit_probe] = runs
    assert exit_probe.synthesizable
    trigger_bar = exit_probe.market_data[exit_probe.trigger_bar_index]
    assert trigger_bar.close < 10.0


# ---------------------------------------------------------------------------
# Unprobeable: exit rule with no entry rule cannot open a position
# ---------------------------------------------------------------------------


def test_exit_probe_without_entry_rule_marks_unprobeable():
    stop = StopLossRule(pct=0.05)
    runs = generate_rule_probe_runs(_spec_with(entry_rules=[], exit_rules=[stop]))
    assert len(runs) == 1
    probe: ProbeRun = runs[0]
    assert not probe.synthesizable
    assert "no_entry_rules" in (probe.unprobeable_reason or "")


# ---------------------------------------------------------------------------
# Symbol resolution
# ---------------------------------------------------------------------------


def test_target_symbols_first_used_as_probe_symbol():
    entry_rule = EntryRule(
        side="long",
        when=Predicate(lhs="bar.close", op=">", rhs=50.0),
    )
    runs = generate_rule_probe_runs(
        _spec_with(entry_rules=[entry_rule], target_symbols=["AAPL", "MSFT"])
    )
    assert runs[0].symbol == "AAPL"


def test_empty_target_symbols_falls_back_to_universe_then_sentinel():
    entry_rule = EntryRule(
        side="long",
        when=Predicate(lhs="bar.close", op=">", rhs=50.0),
    )
    # No compiled code → no UNIVERSE → sentinel.
    runs = generate_rule_probe_runs(_spec_with(entry_rules=[entry_rule]))
    assert runs[0].symbol == "PROBE"
    # Compiled code with UNIVERSE — sentinel becomes the first member.
    code = "UNIVERSE = frozenset({'TEST1', 'TEST2'})\n"
    runs = generate_rule_probe_runs(
        _spec_with(entry_rules=[entry_rule]), compiled_code=code
    )
    assert runs[0].symbol in ("TEST1", "TEST2")


# ---------------------------------------------------------------------------
# Cross-semantics helper unit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "prev_l, cur_l, prev_r, cur_r, op, expected",
    [
        (10.0, 12.0, 11.0, 11.0, "cross_above", True),
        (10.0, 12.0, 11.0, 13.0, "cross_above", False),
        (12.0, 10.0, 11.0, 11.0, "cross_below", True),
        (12.0, 10.0, 11.0, 9.0, "cross_below", False),
        (float("nan"), 12.0, 11.0, 11.0, "cross_above", False),
    ],
)
def test_verify_cross_semantics(prev_l, cur_l, prev_r, cur_r, op, expected):
    assert _verify_cross(prev_l, cur_l, prev_r, cur_r, op) is expected


# ---------------------------------------------------------------------------
# Indicator helper integration — _compute_indicator_at returns floats
# ---------------------------------------------------------------------------


def test_compute_indicator_at_for_sma_returns_finite_float():
    ref = IndicatorRef(name="sma", params={"period": 5})
    bars, trigger, reason = _synthesise_for_predicate(
        Predicate(lhs=ref, op=">", rhs=100.0)
    )
    assert reason is None and bars is not None
    value = _compute_indicator_at(ref, bars, trigger)
    assert value is not None and math.isfinite(value)


def test_compare_semantics_match_compiler_predicate_emitter():
    # Spot-check operator semantics so future refactors don't drift.
    assert _compare(1.0, "<", 2.0) is True
    assert _compare(2.0, "<", 1.0) is False
    assert _compare(2.0, "==", 2.0) is True
    assert _compare(2.0, ">=", 2.0) is True
    assert _compare(1.0, "cross_above", 2.0) is False  # cross_* never matches at the helper level


# ---------------------------------------------------------------------------
# _bars_to_df helper round-trip
# ---------------------------------------------------------------------------


def test_bars_to_df_columns_match_ohlcv():
    entry_rule = EntryRule(
        side="long",
        when=Predicate(lhs="bar.close", op=">", rhs=50.0),
    )
    runs = generate_rule_probe_runs(_spec_with(entry_rules=[entry_rule]))
    df = _bars_to_df(runs[0].market_data)
    assert set(df.columns) == {"open", "high", "low", "close", "volume"}
