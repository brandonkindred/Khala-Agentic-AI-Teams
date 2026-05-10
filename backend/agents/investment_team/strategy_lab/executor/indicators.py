"""Pre-built technical indicators using only pandas and numpy.

Available in the strategy sandbox via: from indicators import <function_name>

Every function takes pd.Series (or multiple Series) and returns pd.Series
or a tuple of pd.Series.  NaN values propagate naturally through pandas
rolling/ewm windows — callers should skip warmup rows where indicators
are NaN.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Callable, Literal, Mapping, Optional, Tuple, Union

import numpy as np
import pandas as pd


def sma(series: pd.Series, period: int) -> pd.Series:
    """Simple Moving Average."""
    return series.rolling(window=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential Moving Average."""
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index (0–100)."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    result = 100 - (100 / (1 + rs))
    # When avg_loss is zero (sustained uptrend) RS is infinite → RSI = 100
    result = result.fillna(np.where(avg_loss == 0, 100.0, np.nan))
    return result


def macd(
    series: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """MACD line, signal line, histogram."""
    fast_ema = ema(series, fast)
    slow_ema = ema(series, slow)
    macd_line = fast_ema - slow_ema
    signal_line = ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def bollinger_bands(
    series: pd.Series,
    period: int = 20,
    num_std: float = 2.0,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Upper band, middle band (SMA), lower band."""
    middle = sma(series, period)
    std = series.rolling(window=period).std()
    upper = middle + num_std * std
    lower = middle - num_std * std
    return upper, middle, lower


def atr(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> pd.Series:
    """Average True Range."""
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.rolling(window=period).mean()


def adx(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> pd.Series:
    """Average Directional Index (0–100).

    Uses Wilder's smoothing (alpha = 1/period) for directional indicators.
    """
    prev_high = high.shift(1)
    prev_low = low.shift(1)

    plus_dm = (high - prev_high).clip(lower=0)
    minus_dm = (prev_low - low).clip(lower=0)

    # Zero out the smaller of the two
    mask_plus = plus_dm < minus_dm
    mask_minus = minus_dm <= plus_dm
    plus_dm = plus_dm.where(~mask_plus, 0)
    minus_dm = minus_dm.where(~mask_minus, 0)

    # Wilder-smoothed True Range (same smoothing as DM to keep ADX consistent)
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    atr_wilder = tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    safe_atr = atr_wilder.replace(0, np.nan)

    plus_di = (
        100 * plus_dm.ewm(alpha=1 / period, min_periods=period, adjust=False).mean() / safe_atr
    )
    minus_di = (
        100 * minus_dm.ewm(alpha=1 / period, min_periods=period, adjust=False).mean() / safe_atr
    )

    di_sum = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / di_sum
    return dx.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def stochastic(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    k_period: int = 14,
    d_period: int = 3,
) -> tuple[pd.Series, pd.Series]:
    """Stochastic Oscillator (%K, %D)."""
    lowest_low = low.rolling(window=k_period).min()
    highest_high = high.rolling(window=k_period).max()
    denom = (highest_high - lowest_low).replace(0, np.nan)
    pct_k = 100 * (close - lowest_low) / denom
    pct_d = pct_k.rolling(window=d_period).mean()
    return pct_k, pct_d


def vwap(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    volume: pd.Series,
) -> pd.Series:
    """Cumulative Volume Weighted Average Price.

    Note: this is a cumulative VWAP with no intraday reset, appropriate
    for daily OHLCV bars.
    """
    typical_price = (high + low + close) / 3
    cum_tp_vol = (typical_price * volume).cumsum()
    cum_vol = volume.cumsum().replace(0, np.nan)
    return cum_tp_vol / cum_vol


# ---------------------------------------------------------------------------
# Indicator registry (#465)
#
# Single source of truth for the static-coverage probe and any future
# probe variant that needs to recognise these helpers by name. Each
# spec carries enough metadata to route an AST ``Call`` through the
# existing input-resolution helpers without per-helper branching:
#
# - ``data_inputs`` lists the positional series-arg slots in declared
#   order. ``"series"`` means a single arbitrary user-supplied series
#   (sma / ema / rsi / macd / bollinger_bands); ``"high"`` /``"low"``
#   /``"close"`` /``"volume"`` mean a fixed OHLCV slot that defaults to
#   the same-named column when the strategy omits it.
# - ``kwarg_names`` lists post-data-input keyword names the helper
#   accepts (used by the probe's ``_resolve_known_kwargs`` to forward
#   strategy-supplied kwargs while declining un-modellable values).
#   Helpers with a positional ``period`` slot still register
#   ``("period",)`` so strategies that write ``sma(close, period=20)``
#   resolve the same way as ``sma(close, 20)``.
# - ``float_kwargs`` lists the names in ``kwarg_names`` that accept a
#   non-integer positive float (e.g. bollinger_bands' ``num_std``);
#   every other scalar slot must resolve to a positive integer.
#   Strategies that supply ``sma(close, 0)`` / ``sma(close, 2.5)``
#   would TypeError at runtime, so the probe declines them rather
#   than letting the helper raise (which would otherwise misclassify
#   the report as ``INDICATOR_FILTER_TOO_RESTRICTIVE`` instead of
#   ``UNKNOWN_LOW_COVERAGE``).
# ---------------------------------------------------------------------------

_DataInputKind = Literal["series", "high", "low", "close", "volume"]


@dataclass(frozen=True)
class IndicatorSpec:
    """Describes how the static-coverage probe should resolve an indicator call.

    The ``helper`` is the runtime function from this module. ``data_inputs``
    is iterated in declared positional order so the dispatcher can
    forward each AST positional arg (or fall back to the OHLCV default
    column) without per-helper branches.
    """

    helper: Callable[..., Union[pd.Series, Tuple[pd.Series, ...]]]
    data_inputs: Tuple[_DataInputKind, ...]
    kwarg_names: Tuple[str, ...]
    tuple_arity: Optional[int]
    # Names in ``kwarg_names`` whose value the helper accepts as a
    # non-integer positive float (e.g. bollinger_bands' ``num_std``).
    # Every other scalar must resolve to a positive integer or the
    # dispatcher declines the indicator.
    float_kwargs: frozenset = frozenset()


INDICATORS: Mapping[str, IndicatorSpec] = MappingProxyType(
    {
        "sma": IndicatorSpec(
            helper=sma, data_inputs=("series",), kwarg_names=("period",), tuple_arity=None
        ),
        "ema": IndicatorSpec(
            helper=ema, data_inputs=("series",), kwarg_names=("period",), tuple_arity=None
        ),
        "rsi": IndicatorSpec(
            helper=rsi, data_inputs=("series",), kwarg_names=("period",), tuple_arity=None
        ),
        "atr": IndicatorSpec(
            helper=atr,
            data_inputs=("high", "low", "close"),
            kwarg_names=("period",),
            tuple_arity=None,
        ),
        "adx": IndicatorSpec(
            helper=adx,
            data_inputs=("high", "low", "close"),
            kwarg_names=("period",),
            tuple_arity=None,
        ),
        "vwap": IndicatorSpec(
            helper=vwap,
            data_inputs=("high", "low", "close", "volume"),
            kwarg_names=(),
            tuple_arity=None,
        ),
        "macd": IndicatorSpec(
            helper=macd,
            data_inputs=("series",),
            kwarg_names=("fast", "slow", "signal"),
            tuple_arity=3,
        ),
        "bollinger_bands": IndicatorSpec(
            helper=bollinger_bands,
            data_inputs=("series",),
            kwarg_names=("period", "num_std"),
            tuple_arity=3,
            float_kwargs=frozenset({"num_std"}),
        ),
        "stochastic": IndicatorSpec(
            helper=stochastic,
            data_inputs=("high", "low", "close"),
            kwarg_names=("k_period", "d_period"),
            tuple_arity=2,
        ),
    }
)
