"""Pure-Python factor primitives — reference implementations.

Each function consumes ``bars``, a list of bar-like objects with
``.open / .high / .low / .close / .volume`` attributes (the
:class:`contract.Bar` shape), and returns the primitive's value AT THE
LAST bar in the list.  NaN is returned when there is insufficient history.

These reference implementations exist for two reasons:

* Direct unit testing — :file:`tests/test_factor_dsl.py` calls them with
  synthetic bar lists to verify purity, determinism, and warm-up handling.
* Documentation — they double as the spec the compiler templates in
  :file:`compiler.py` are checked against.

The OHLCV-derivable primitives delegate to
:class:`strategy_lab.indicators.streaming.IndicatorRegistry` — the
canonical, host-side recurrence implementation. The compiler still emits
inline templated bodies (the sandbox import whitelist forbids
non-``contract`` / non-``math`` imports), but the templates are kept in
lock-step with the registry by ``tests/test_streaming_indicators.py``.
"""

from __future__ import annotations

import math
from typing import Any, Optional, Sequence

from ..indicators.streaming import IndicatorRegistry, macd_components, windowed_ema

NAN = float("nan")


def _isnan(x: float) -> bool:
    try:
        return math.isnan(x)
    except (TypeError, ValueError):
        return False


def _or_nan(value: Optional[float]) -> float:
    return NAN if value is None else float(value)


# ---------------------------------------------------------------------------
# OHLCV-derivable primitives.
# ---------------------------------------------------------------------------


def price(bars: Sequence[Any], field: str = "close") -> float:
    if not bars:
        return NAN
    return float(getattr(bars[-1], field))


def const(bars: Sequence[Any], value: float) -> float:  # noqa: ARG001 — uniform sig
    return float(value)


def sma(bars: Sequence[Any], period: int) -> float:
    if len(bars) < period:
        return NAN
    return sum(b.close for b in bars[-period:]) / period


def ema(bars: Sequence[Any], period: int) -> float:
    if len(bars) < period:
        return NAN
    return windowed_ema(bars, period, "close")


def rsi(bars: Sequence[Any], period: int = 14) -> float:
    return _or_nan(IndicatorRegistry().rsi(bars, period=period, source="close"))


def macd_signal(bars: Sequence[Any], fast: int, slow: int, signal: int) -> float:
    """EMA-of-EMA-difference signal line.

    Warm-up threshold is ``slow + signal - 1`` — the smallest ``len(bars)``
    at which ``len(macd_line) == signal`` and the signal-EMA fills.
    Matches :func:`macd_components`, :meth:`IndicatorRegistry.macd`, and
    both compiler templates so the 'canonical reference implementation'
    contract documented at :mod:`strategy_lab.indicators` holds at the
    warm-up boundary.
    """
    if len(bars) < slow + signal - 1:
        return NAN
    _, sig, _ = macd_components(bars, fast=fast, slow=slow, signal=signal, source="close")
    return _or_nan(sig)


def bollinger_z(bars: Sequence[Any], period: int) -> float:
    """Z-score of close relative to ``period``-bar mean and stdev."""
    if len(bars) < period:
        return NAN
    window = [b.close for b in bars[-period:]]
    mean = sum(window) / period
    var = sum((x - mean) ** 2 for x in window) / period
    if var <= 0:
        return 0.0
    std = math.sqrt(var)
    return (bars[-1].close - mean) / std


def atr(bars: Sequence[Any], period: int = 14) -> float:
    return _or_nan(IndicatorRegistry().atr(bars, period=period))


def adx(bars: Sequence[Any], period: int = 14) -> float:
    """Wilder's ADX, smoothed once."""
    return _or_nan(IndicatorRegistry().adx(bars, period=period))


def stochastic_k(bars: Sequence[Any], period: int = 14) -> float:
    if len(bars) < period:
        return NAN
    window = bars[-period:]
    lowest = min(b.low for b in window)
    highest = max(b.high for b in window)
    rng = highest - lowest
    if rng == 0:
        return 50.0
    return 100.0 * (bars[-1].close - lowest) / rng


def vwap(bars: Sequence[Any], period: int) -> float:
    if len(bars) < period:
        return NAN
    window = bars[-period:]
    num = sum(((b.high + b.low + b.close) / 3.0) * b.volume for b in window)
    den = sum(b.volume for b in window)
    if den == 0:
        return sum(b.close for b in window) / period
    return num / den


def momentum_k(bars: Sequence[Any], k: int) -> float:
    """k-bar log-return divided by k-bar realised stdev of returns.

    Returns 0 when there is no realised volatility to normalise by; NaN
    when there is insufficient history.
    """
    if len(bars) < k + 1:
        return NAN
    ret = math.log(bars[-1].close / bars[-1 - k].close)
    rets = [math.log(bars[i].close / bars[i - 1].close) for i in range(len(bars) - k, len(bars))]
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    if var <= 0:
        return 0.0
    return ret / math.sqrt(var * k)


def zscore_residual_ols(
    bars: Sequence[Any],
    window: int,
    vs_bars: Optional[Sequence[Any]],
) -> float:
    """Z-score of OLS residual ``close ~ vs_close`` over the trailing window.

    ``vs_bars`` is the same-length, time-aligned series for ``vs_symbol``.
    NaN is returned when the cross-symbol series isn't supplied (the
    orchestrator-side fetch will provide it; tests pass a stub list).
    """
    if vs_bars is None or len(bars) < window or len(vs_bars) < window:
        return NAN
    y = [b.close for b in bars[-window:]]
    x = [b.close for b in vs_bars[-window:]]
    n = len(y)
    mean_x = sum(x) / n
    mean_y = sum(y) / n
    cov = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y))
    var_x = sum((xi - mean_x) ** 2 for xi in x)
    if var_x <= 0:
        return NAN
    beta = cov / var_x
    alpha = mean_y - beta * mean_x
    residuals = [yi - (alpha + beta * xi) for xi, yi in zip(x, y)]
    mean_r = sum(residuals) / n
    var_r = sum((r - mean_r) ** 2 for r in residuals) / n
    if var_r <= 0:
        return 0.0
    std_r = math.sqrt(var_r)
    return residuals[-1] / std_r


def skew(bars: Sequence[Any], window: int) -> float:
    if len(bars) < window + 1:
        return NAN
    rets = [
        math.log(bars[i].close / bars[i - 1].close) for i in range(len(bars) - window, len(bars))
    ]
    n = len(rets)
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / n
    if var <= 0:
        return 0.0
    std = math.sqrt(var)
    return (sum((r - mean) ** 3 for r in rets) / n) / (std**3)


def vol_regime_state(bars: Sequence[Any], lookback: int, threshold: float) -> float:
    """Return a discrete regime label: 0 (low), 1 (mid), 2 (high).

    Compares short-window realised vol (``lookback / 4``, min 5) to the
    long-window realised vol (``lookback``).  ``threshold`` is the band
    around 1.0 used to separate the three buckets.
    """
    short = max(5, lookback // 4)
    if len(bars) < lookback + 1:
        return NAN
    long_rets = [
        math.log(bars[i].close / bars[i - 1].close) for i in range(len(bars) - lookback, len(bars))
    ]
    short_rets = long_rets[-short:]
    long_var = sum(r * r for r in long_rets) / len(long_rets)
    short_var = sum(r * r for r in short_rets) / len(short_rets)
    if long_var <= 0:
        return 1.0
    ratio = math.sqrt(short_var / long_var)
    if ratio < 1.0 / threshold:
        return 0.0
    if ratio > threshold:
        return 2.0
    return 1.0


# ---------------------------------------------------------------------------
# Cross-asset primitives — return NaN until the aux feed lands (issue #249
# follow-up).  Genomes that gate entries on these primitives simply will not
# fire signals against the stub feed; this is intentional and lets the
# schema be future-ready while keeping the harness boundary unchanged.
# ---------------------------------------------------------------------------


def term_structure_slope(
    bars: Sequence[Any],  # noqa: ARG001 — uniform sig
    aux: Optional[Any],
    window: int,  # noqa: ARG001
) -> float:
    if aux is None:
        return NAN
    return float("nan")  # real implementation lands with the cross-asset provider


def funding_rate_deviation(
    bars: Sequence[Any],  # noqa: ARG001
    aux: Optional[Any],
    lookback: int,  # noqa: ARG001
) -> float:
    if aux is None:
        return NAN
    return float("nan")
