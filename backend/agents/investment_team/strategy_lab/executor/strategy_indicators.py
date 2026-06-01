"""Scalar-returning indicator API exposed to strategy ``on_bar`` code.

Strategy code does ``from indicators import ema, sma, ...`` and uses the
result directly in a comparison (``if ema(history, 20) > bar.close``), so each
helper here returns the **latest scalar value** of the underlying rolling
indicator — a ``float`` for single-output indicators, or a tuple of floats for
multi-output ones (``macd``, ``bollinger_bands``, ``stochastic``).

This is the single source of truth shared by the two execution paths that run
generated strategy code:

* the streaming sandbox copies this module in as ``indicators.py`` (alongside
  the Series-returning implementation as ``_indicators_impl.py``), and
* the predicate-conformance shadow gate imports it in-process,

so a strategy sees one identical, scalar contract in both. The Series-returning
``executor.indicators`` module stays the implementation used internally by the
engine (predicate evaluator, rule probes) and is *not* exposed to strategy code
directly.

Module invariant: every public helper returns the most recent value of the
corresponding ``executor.indicators`` Series (NaN/empty warm-up → ``0.0``);
none of them returns a ``pd.Series``.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

try:  # in-package use (predicate-conformance gate, in-process tests)
    from . import indicators as _impl
except ImportError:  # flat sandbox layout: harness copies the impl as _indicators_impl.py
    import _indicators_impl as _impl  # type: ignore[no-redef]


def _last(series: pd.Series) -> float:
    """Return the most recent finite value of ``series`` (warm-up → 0.0).

    Preconditions:
        ``series`` is a ``pd.Series`` (as returned by an ``executor.indicators``
        helper).
    Postconditions:
        Returns a ``float``: the last element, or ``0.0`` when the series is
        empty or its last value is ``None``/``NaN``.
    """
    if series.empty:
        return 0.0
    val = series.iloc[-1]
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return 0.0
    return float(val)


def sma(data, period) -> float:
    """Latest Simple Moving Average value. See module contract."""
    return _last(_impl.sma(data, int(period)))


def ema(data, period) -> float:
    """Latest Exponential Moving Average value. See module contract."""
    return _last(_impl.ema(data, int(period)))


def rsi(data, period=14) -> float:
    """Latest Relative Strength Index value. See module contract."""
    return _last(_impl.rsi(data, int(period)))


def macd(data, fast=12, slow=26, signal=9) -> tuple[float, float, float]:
    """Latest (MACD line, signal line, histogram) values. See module contract."""
    macd_line, signal_line, hist = _impl.macd(
        data, fast=int(fast), slow=int(slow), signal=int(signal)
    )
    return _last(macd_line), _last(signal_line), _last(hist)


def bollinger_bands(data, period=20, num_std=2.0) -> tuple[float, float, float]:
    """Latest (upper, middle, lower) Bollinger Band values. See module contract."""
    upper, middle, lower = _impl.bollinger_bands(
        data, period=int(period), num_std=float(num_std)
    )
    return _last(upper), _last(middle), _last(lower)


def atr(high, low, close, period=14) -> float:
    """Latest Average True Range value. See module contract."""
    return _last(_impl.atr(high, low, close, period=int(period)))


def adx(high, low, close, period=14) -> float:
    """Latest Average Directional Index value. See module contract."""
    return _last(_impl.adx(high, low, close, period=int(period)))


def stochastic(high, low, close, k_period=14, d_period=3) -> tuple[float, float]:
    """Latest (%K, %D) Stochastic Oscillator values. See module contract."""
    pct_k, pct_d = _impl.stochastic(
        high, low, close, k_period=int(k_period), d_period=int(d_period)
    )
    return _last(pct_k), _last(pct_d)


def vwap(high, low, close, volume) -> float:
    """Latest cumulative VWAP value. See module contract."""
    return _last(_impl.vwap(high, low, close, volume))
