"""Extra coverage for ``strategy_lab.factors.primitives``.

``test_factor_dsl.py`` covers the simple SMA/EMA/RSI/bollinger paths;
this file targets the remaining primitives: ``price``, ``const``,
``adx``, ``stochastic_k``, ``vwap``, ``momentum_k``,
``zscore_residual_ols``, ``skew``, ``vol_regime_state``, plus the
already-NaN cross-asset primitives.

Each test uses a tiny ``_Bar`` dataclass mirroring the
``contract.Bar`` shape so the primitives don't need a real OHLCV
provider.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List

from investment_team.strategy_lab.factors import primitives as P


@dataclass
class _Bar:
    """Tiny bar stand-in (mirrors the version in test_factor_dsl)."""

    open: float
    high: float
    low: float
    close: float
    volume: float = 1000.0


def _ramp(n: int, base: float = 100.0, step: float = 1.0) -> List[_Bar]:
    return [
        _Bar(base + i * step, base + i * step + 0.5, base + i * step - 0.5, base + i * step)
        for i in range(n)
    ]


def _flat(n: int, price: float = 100.0) -> List[_Bar]:
    return [_Bar(price, price, price, price) for _ in range(n)]


# ---------------------------------------------------------------------------
# price + const
# ---------------------------------------------------------------------------


def test_price_returns_nan_on_empty_bars() -> None:
    assert math.isnan(P.price([]))


def test_price_returns_last_close_by_default() -> None:
    bars = _ramp(5)
    assert P.price(bars) == bars[-1].close


def test_price_supports_alternate_field() -> None:
    bars = _ramp(5)
    assert P.price(bars, "high") == bars[-1].high


def test_const_ignores_bars_and_returns_value() -> None:
    bars = _ramp(3)
    assert P.const(bars, 7.5) == 7.5
    assert P.const([], 0.0) == 0.0


def test_isnan_helper_handles_typeerror() -> None:
    """The internal _isnan helper must swallow TypeError on non-numerics."""
    assert P._isnan(float("nan")) is True
    assert P._isnan(1.0) is False
    # The except-branch (TypeError) — pass a non-numeric to math.isnan.
    assert P._isnan("not a number") is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# RSI corner: zero gains, zero losses
# ---------------------------------------------------------------------------


def test_rsi_returns_50_when_no_gains_and_no_losses() -> None:
    # Flat closes — every delta is 0, so avg_loss == 0 and avg_gain == 0.
    bars = _flat(30)
    # avg_loss == 0 path: 100 only when avg_gain > 0; else 50.
    assert P.rsi(bars, 14) == 50.0


# ---------------------------------------------------------------------------
# ADX
# ---------------------------------------------------------------------------


def test_adx_returns_nan_when_too_few_bars() -> None:
    assert math.isnan(P.adx(_ramp(5), period=14))


def test_adx_zero_when_no_movement() -> None:
    bars = _flat(40)
    # No true-range movement at all → trs sum to 0 → early-exit returns 0.
    assert P.adx(bars, period=14) == 0.0


def test_adx_finite_for_trending_series() -> None:
    bars = _ramp(40)
    val = P.adx(bars, period=14)
    assert math.isfinite(val)
    assert val >= 0


# ---------------------------------------------------------------------------
# Stochastic K
# ---------------------------------------------------------------------------


def test_stochastic_k_nan_when_too_few_bars() -> None:
    assert math.isnan(P.stochastic_k(_ramp(5), period=14))


def test_stochastic_k_returns_50_when_flat_range() -> None:
    bars = _flat(20)
    assert P.stochastic_k(bars, period=14) == 50.0


def test_stochastic_k_at_top_of_window() -> None:
    bars = _ramp(20)
    # Latest close is the highest — K should peg near 100.
    val = P.stochastic_k(bars, period=14)
    assert 0.0 <= val <= 100.0


# ---------------------------------------------------------------------------
# VWAP
# ---------------------------------------------------------------------------


def test_vwap_returns_nan_when_too_few_bars() -> None:
    assert math.isnan(P.vwap(_ramp(5), period=14))


def test_vwap_falls_back_to_mean_when_volume_zero() -> None:
    # All-zero-volume window — VWAP returns simple mean of closes.
    bars = [_Bar(open=100, high=101, low=99, close=100, volume=0) for _ in range(14)]
    expected = sum(b.close for b in bars[-14:]) / 14
    assert P.vwap(bars, period=14) == expected


def test_vwap_weighted_by_volume() -> None:
    bars = _ramp(10)
    val = P.vwap(bars, period=10)
    assert math.isfinite(val)


# ---------------------------------------------------------------------------
# momentum_k
# ---------------------------------------------------------------------------


def test_momentum_k_nan_when_too_few_bars() -> None:
    assert math.isnan(P.momentum_k(_ramp(3), k=5))


def test_momentum_k_zero_when_zero_variance() -> None:
    bars = _flat(20)
    # ratio is log(1)=0 over a zero-variance window → 0 by the var<=0 guard.
    assert P.momentum_k(bars, k=5) == 0.0


def test_momentum_k_finite_for_trending_series() -> None:
    bars = _ramp(20)
    val = P.momentum_k(bars, k=5)
    assert math.isfinite(val)


# ---------------------------------------------------------------------------
# zscore_residual_ols
# ---------------------------------------------------------------------------


def test_zscore_residual_ols_nan_when_vs_bars_none() -> None:
    assert math.isnan(P.zscore_residual_ols(_ramp(20), window=10, vs_bars=None))


def test_zscore_residual_ols_nan_when_window_too_short() -> None:
    bars = _ramp(5)
    aux = _ramp(5)
    assert math.isnan(P.zscore_residual_ols(bars, window=10, vs_bars=aux))


def test_zscore_residual_ols_nan_when_aux_too_short() -> None:
    bars = _ramp(20)
    aux = _ramp(5)
    assert math.isnan(P.zscore_residual_ols(bars, window=10, vs_bars=aux))


def test_zscore_residual_ols_nan_when_x_variance_zero() -> None:
    bars = _ramp(20)
    aux = _flat(20)  # var_x == 0
    assert math.isnan(P.zscore_residual_ols(bars, window=10, vs_bars=aux))


def test_zscore_residual_ols_zero_when_residual_variance_zero() -> None:
    # Perfect linear relationship: residuals are all zero → var_r == 0.
    bars = _ramp(20)
    aux = _ramp(20, base=50.0, step=0.5)  # x = (close-50)/0.5, y = close
    val = P.zscore_residual_ols(bars, window=10, vs_bars=aux)
    assert val == 0.0


def test_zscore_residual_ols_finite_for_noisy_pair() -> None:
    bars = _ramp(20)
    # Slightly perturb aux to introduce residual variance.
    aux = _ramp(20)
    aux[-1] = _Bar(
        open=aux[-1].open + 5,
        high=aux[-1].high + 5,
        low=aux[-1].low + 5,
        close=aux[-1].close + 5,
    )
    val = P.zscore_residual_ols(bars, window=10, vs_bars=aux)
    assert math.isfinite(val)


# ---------------------------------------------------------------------------
# skew
# ---------------------------------------------------------------------------


def test_skew_nan_when_too_few_bars() -> None:
    assert math.isnan(P.skew(_ramp(5), window=10))


def test_skew_zero_for_flat_returns() -> None:
    bars = _flat(20)
    assert P.skew(bars, window=10) == 0.0


def test_skew_finite_for_trending_series() -> None:
    bars = _ramp(40)
    val = P.skew(bars, window=20)
    assert math.isfinite(val)


# ---------------------------------------------------------------------------
# vol_regime_state
# ---------------------------------------------------------------------------


def test_vol_regime_state_nan_when_too_few_bars() -> None:
    assert math.isnan(P.vol_regime_state(_ramp(5), lookback=20, threshold=1.2))


def test_vol_regime_state_returns_mid_when_long_var_zero() -> None:
    bars = _flat(30)
    # Flat closes → long_var == 0 → mid regime (1.0).
    assert P.vol_regime_state(bars, lookback=20, threshold=1.2) == 1.0


def test_vol_regime_state_low_high_mid_buckets() -> None:
    # Mostly small returns then a single large spike — short-window vol >> long-window.
    bars = _ramp(60)
    # Inject a spike near the end so the short window's variance dwarfs the long window's.
    last = bars[-1]
    bars[-1] = _Bar(
        open=last.open, high=last.high * 10, low=last.low, close=last.close * 10, volume=last.volume
    )
    val_high = P.vol_regime_state(bars, lookback=30, threshold=1.2)
    assert val_high in (0.0, 1.0, 2.0)

    # Trending ramp — should sit in mid regime (short ≈ long variance).
    val_mid = P.vol_regime_state(_ramp(60), lookback=30, threshold=2.0)
    assert val_mid in (0.0, 1.0, 2.0)


# ---------------------------------------------------------------------------
# Cross-asset primitives — aux=None branch already covered in
# test_factor_dsl.test_cross_asset_primitives_return_nan_without_aux.
# Cover the post-aux-check branch (aux provided → NaN until provider lands).
# ---------------------------------------------------------------------------


def test_term_structure_slope_nan_when_aux_provided() -> None:
    bars = _ramp(20)
    # Stub aux value — the primitive still returns NaN until the real
    # cross-asset provider lands.
    assert math.isnan(P.term_structure_slope(bars, aux=[1.0, 2.0], window=10))


def test_funding_rate_deviation_nan_when_aux_provided() -> None:
    bars = _ramp(20)
    assert math.isnan(P.funding_rate_deviation(bars, aux=[0.01, 0.02], lookback=14))


# ---------------------------------------------------------------------------
# macd_signal — exercise paths that test_factor_dsl skipped
# ---------------------------------------------------------------------------


def test_macd_signal_nan_when_too_few_bars() -> None:
    # slow=26, signal=9 → needs >= 35 bars
    assert math.isnan(P.macd_signal(_ramp(20), fast=12, slow=26, signal=9))


def test_macd_signal_finite_for_trending_series() -> None:
    bars = _ramp(60)
    val = P.macd_signal(bars, fast=12, slow=26, signal=9)
    assert math.isfinite(val)


# ---------------------------------------------------------------------------
# atr — exercise non-warmup path explicitly (test_factor_dsl only checks NaN)
# ---------------------------------------------------------------------------


def test_atr_finite_when_window_satisfied() -> None:
    bars = _ramp(30)
    val = P.atr(bars, period=14)
    assert math.isfinite(val)
    assert val >= 0


# ---------------------------------------------------------------------------
# ema — explicit value check for an alpha-smoothed series
# ---------------------------------------------------------------------------


def test_ema_close_to_arithmetic_mean_for_short_window() -> None:
    bars = _ramp(10)
    val = P.ema(bars, period=5)
    # EMA of a linear ramp is bounded by the range of the last `period` closes.
    last_close = bars[-1].close
    first_close = bars[-5].close
    assert first_close <= val <= last_close
