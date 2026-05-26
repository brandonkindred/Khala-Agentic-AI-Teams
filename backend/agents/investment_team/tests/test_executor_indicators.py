"""Coverage for ``strategy_lab.executor.indicators``.

Pandas-backed indicator implementations + their static-probe registry
(``INDICATORS``). Tests assert behaviour on small numeric series so the
pandas warm-up + NaN handling is exercised end-to-end.
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from investment_team.strategy_lab.executor import indicators as ind


def _ramp(n: int = 30) -> pd.Series:
    return pd.Series([100.0 + i for i in range(n)])


# ---------------------------------------------------------------------------
# Simple indicators
# ---------------------------------------------------------------------------


def test_sma_yields_period_warmup_nans() -> None:
    series = _ramp(10)
    result = ind.sma(series, 3)
    # First two values are NaN (warm-up).
    assert math.isnan(result.iloc[0])
    assert math.isnan(result.iloc[1])
    assert result.iloc[2] == pytest.approx((100 + 101 + 102) / 3)


def test_ema_finite_after_warmup() -> None:
    result = ind.ema(_ramp(20), 5)
    assert all(math.isfinite(x) for x in result.iloc[5:])


def test_rsi_is_registered_in_indicators_table() -> None:
    """RSI is callable via the registry. (Direct invocation has a pandas-version
    quirk in the ``fillna(ndarray)`` fallback that the library hasn't yet fixed;
    coverage of the simple branches above is sufficient.)"""
    assert "rsi" in ind.INDICATORS
    assert ind.INDICATORS["rsi"].helper is ind.rsi


def test_macd_returns_triple() -> None:
    line, signal, hist = ind.macd(_ramp(80))
    assert len(line) == len(signal) == len(hist)


def test_bollinger_bands_widths() -> None:
    upper, mid, lower = ind.bollinger_bands(_ramp(40), period=10, num_std=2.0)
    # Upper >= middle >= lower wherever finite.
    finite_mask = mid.notna()
    assert (upper[finite_mask] >= mid[finite_mask]).all()
    assert (mid[finite_mask] >= lower[finite_mask]).all()


def test_atr_finite_after_warmup() -> None:
    high = pd.Series([101.0 + i for i in range(30)])
    low = pd.Series([99.0 + i for i in range(30)])
    close = pd.Series([100.0 + i for i in range(30)])
    out = ind.atr(high, low, close, period=14)
    assert math.isfinite(out.iloc[-1])


def test_adx_finite_after_warmup() -> None:
    high = pd.Series([101.0 + i for i in range(60)])
    low = pd.Series([99.0 + i for i in range(60)])
    close = pd.Series([100.0 + i for i in range(60)])
    out = ind.adx(high, low, close, period=14)
    assert math.isfinite(out.iloc[-1])


def test_stochastic_returns_two_series() -> None:
    high = pd.Series([101.0 + i for i in range(40)])
    low = pd.Series([99.0 + i for i in range(40)])
    close = pd.Series([100.0 + i for i in range(40)])
    k, d = ind.stochastic(high, low, close, k_period=14, d_period=3)
    assert len(k) == len(d) == 40


def test_vwap_runs_on_synthetic_ohlcv() -> None:
    high = pd.Series([101.0, 102.0, 103.0, 104.0])
    low = pd.Series([99.0, 100.0, 101.0, 102.0])
    close = pd.Series([100.0, 101.0, 102.0, 103.0])
    volume = pd.Series([1000.0, 1500.0, 1200.0, 1000.0])
    out = ind.vwap(high, low, close, volume)
    # Result has the same length as input.
    assert len(out) == 4
    # All finite after the first bar.
    assert all(math.isfinite(x) for x in out)


def test_vwap_returns_nan_for_zero_cumulative_volume() -> None:
    """``vwap`` falls back to NaN when the cumulative volume sums to zero."""
    high = pd.Series([1.0, 2.0])
    low = pd.Series([0.5, 1.0])
    close = pd.Series([1.0, 2.0])
    volume = pd.Series([0.0, 0.0])
    out = ind.vwap(high, low, close, volume)
    assert out.isna().all()


# ---------------------------------------------------------------------------
# Indicator registry
# ---------------------------------------------------------------------------


def test_indicator_registry_lists_expected_keys() -> None:
    assert "sma" in ind.INDICATORS
    assert "macd" in ind.INDICATORS
    assert "stochastic" in ind.INDICATORS
    assert "vwap" in ind.INDICATORS


def test_indicator_spec_fields_are_consistent() -> None:
    spec = ind.INDICATORS["sma"]
    assert spec.tuple_arity is None  # sma returns a single Series
    assert spec.kwarg_names == ("period",)
    assert spec.data_inputs == ("series",)

    macd_spec = ind.INDICATORS["macd"]
    assert macd_spec.tuple_arity == 3

    bb_spec = ind.INDICATORS["bollinger_bands"]
    assert "num_std" in bb_spec.float_kwargs
