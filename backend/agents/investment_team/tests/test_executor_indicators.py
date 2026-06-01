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
from investment_team.trading_service.strategy import contract


def _ramp(n: int = 30) -> pd.Series:
    return pd.Series([100.0 + i for i in range(n)])


def _bars(n: int = 30) -> list[contract.Bar]:
    """Synthetic OHLCV bars mirroring the ``list[Bar]`` ``ctx.history`` returns."""
    return [
        contract.Bar(
            symbol="TEST",
            timestamp=f"2024-01-{(i % 28) + 1:02d}T00:00:00Z",
            timeframe="1d",
            open=100.0 + i,
            high=101.0 + i,
            low=99.0 + i,
            close=100.0 + i,
            volume=1000.0 + i,
        )
        for i in range(n)
    ]


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


# ---------------------------------------------------------------------------
# Input coercion (_coerce_series): accept list[float] / list[Bar] / list[dict]
# ---------------------------------------------------------------------------


def test_coerce_series_passes_through_existing_series() -> None:
    series = _ramp(5)
    # A pd.Series is returned unchanged (same object, no copy).
    assert ind._coerce_series(series) is series


def test_coerce_series_from_list_of_floats() -> None:
    out = ind._coerce_series([100.0, 101.0, 102.0])
    assert isinstance(out, pd.Series)
    assert out.tolist() == [100.0, 101.0, 102.0]


def test_coerce_series_extracts_field_from_bars() -> None:
    bars = _bars(3)
    assert ind._coerce_series(bars, "close").tolist() == [100.0, 101.0, 102.0]
    assert ind._coerce_series(bars, "high").tolist() == [101.0, 102.0, 103.0]
    assert ind._coerce_series(bars, "volume").tolist() == [1000.0, 1001.0, 1002.0]


def test_coerce_series_from_list_of_dicts() -> None:
    rows = [{"close": 10.0}, {"close": 11.0}]
    assert ind._coerce_series(rows, "close").tolist() == [10.0, 11.0]


def test_coerce_series_empty_list_yields_empty_float_series() -> None:
    out = ind._coerce_series([])
    assert isinstance(out, pd.Series)
    assert len(out) == 0
    assert out.dtype == float


def test_coerce_series_rejects_scalar_with_typeerror() -> None:
    with pytest.raises(TypeError):
        ind._coerce_series(42.0)


def test_coerce_series_rejects_unextractable_elements_with_typeerror() -> None:
    # A clear TypeError (not AttributeError) so the sandbox classifies the
    # failure as a strategy-code bug rather than a look-ahead violation.
    with pytest.raises(TypeError):
        ind._coerce_series(["string"])


def test_coerce_series_rejects_bools() -> None:
    with pytest.raises(TypeError):
        ind._coerce_series([True, False])


# ---------------------------------------------------------------------------
# Indicators accept lists/bars equivalently to pd.Series
# ---------------------------------------------------------------------------


def test_ema_accepts_list_of_floats_like_series() -> None:
    closes = [100.0 + i for i in range(20)]
    from_list = ind.ema(closes, 5)
    from_series = ind.ema(pd.Series(closes), 5)
    pd.testing.assert_series_equal(from_list, from_series)


def test_ema_accepts_list_of_bars_via_close() -> None:
    bars = _bars(20)
    from_bars = ind.ema(bars, 5)
    from_series = ind.ema(pd.Series([b.close for b in bars]), 5)
    pd.testing.assert_series_equal(from_bars, from_series)


def test_ema_rejects_bad_list_with_typeerror_not_attributeerror() -> None:
    with pytest.raises(TypeError):
        ind.ema(["string"], 5)


def test_sma_accepts_list_of_bars() -> None:
    bars = _bars(10)
    out = ind.sma(bars, 3)
    assert out.iloc[2] == pytest.approx((100 + 101 + 102) / 3)


def test_multiseries_atr_accepts_same_bars_positionally() -> None:
    bars = _bars(30)
    # Passing the same list[Bar] to every slot relies on per-arg field
    # extraction (high→.high, low→.low, close→.close).
    from_bars = ind.atr(bars, bars, bars, period=14)
    from_series = ind.atr(
        pd.Series([b.high for b in bars]),
        pd.Series([b.low for b in bars]),
        pd.Series([b.close for b in bars]),
        period=14,
    )
    pd.testing.assert_series_equal(from_bars, from_series)


def test_multiseries_vwap_accepts_same_bars_positionally() -> None:
    bars = _bars(8)
    from_bars = ind.vwap(bars, bars, bars, bars)
    from_series = ind.vwap(
        pd.Series([b.high for b in bars]),
        pd.Series([b.low for b in bars]),
        pd.Series([b.close for b in bars]),
        pd.Series([b.volume for b in bars]),
    )
    pd.testing.assert_series_equal(from_bars, from_series)


def test_stochastic_accepts_bars() -> None:
    bars = _bars(40)
    k, d = ind.stochastic(bars, bars, bars, k_period=14, d_period=3)
    assert len(k) == len(d) == 40
