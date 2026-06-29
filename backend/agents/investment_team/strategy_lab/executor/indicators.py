"""Pre-built technical indicators using only pandas and numpy.

Available in the strategy sandbox via: from indicators import <function_name>

Every function accepts either a ``pd.Series`` or a sequence the strategy
already has on hand — a ``list[float]`` or the ``list[Bar]`` that
``ctx.history(symbol, n)`` returns — and returns ``pd.Series`` or a tuple of
``pd.Series``.  Inputs are coerced at the boundary (see ``_coerce_series``),
so callers do not need to wrap price data in ``pd.Series`` themselves.  NaN
values propagate naturally through pandas rolling/ewm windows — callers should
skip warmup rows where indicators are NaN.
"""

from __future__ import annotations

import numbers
from dataclasses import dataclass
from types import MappingProxyType
from typing import Callable, Literal, Mapping, Optional, Tuple, Union

import numpy as np
import pandas as pd


def _coerce_series(series, field: str = "close") -> pd.Series:
    """Coerce a caller-supplied price/volume sequence into a ``pd.Series``.

    Accepts a ``pd.Series`` (returned unchanged), a ``list``/``tuple`` of
    numbers, a ``list``/``tuple`` of bar objects exposing ``field`` (e.g. the
    ``Bar`` records returned by ``ctx.history``), or a ``list``/``tuple`` of
    dicts containing ``field``.  This is the single boundary at which the
    indicator API absorbs the variety of shapes strategy code may pass.

    Preconditions:
        ``series`` is a ``pd.Series`` or a ``list``/``tuple`` whose elements
        are numbers, objects exposing the ``field`` attribute, or dicts
        containing the ``field`` key.
    Postconditions:
        Returns a float ``pd.Series``; an empty list/tuple yields an empty
        float Series.  A numeric ``pd.Series`` is returned as-is (no copy);
        an object-dtype Series is rebuilt with float values but keeps the
        caller's index so downstream alignment is preserved.  Every element is
        validated (not just the first), and any contract violation raises
        ``TypeError`` — never ``AttributeError``/``KeyError`` — so the sandbox
        classifies bad input as a strategy-code error rather than a look-ahead
        violation.
    """
    if isinstance(series, pd.Series):
        # A numeric Series passes straight through (preserving its index). A
        # bool-dtype Series is rejected — True/False are never meaningful
        # prices, matching the per-element bool rejection below. An
        # object-dtype Series may still hold Bar/dict/number elements — e.g. a
        # caller wrapped list[Bar] with pd.Series(...) before reaching here —
        # so coerce its values while keeping the caller's (possibly datetime)
        # index rather than rebuilding a fresh RangeIndex.
        if pd.api.types.is_bool_dtype(series):
            raise TypeError("indicator input must be numeric, got bool Series")
        if series.dtype != object:
            return series
        return _coerce_series(list(series), field).set_axis(series.index)
    # Accept any non-string sequence/iterable (list, tuple, collections.deque,
    # np.ndarray, …) by materialising it once; reject strings and scalars.
    if isinstance(series, (str, bytes, bytearray)) or not hasattr(series, "__iter__"):
        raise TypeError(
            f"indicator input must be pd.Series or a sequence, got {type(series).__name__}"
        )
    if not isinstance(series, (list, tuple)):
        series = list(series)
    if not series:
        return pd.Series([], dtype=float)
    first = series[0]
    if isinstance(first, numbers.Real):
        # numbers.Real covers Python int/float and numpy numeric scalars
        # (np.int64, np.float32, …), so a non-float64 ndarray coerces correctly.
        # Python bool is a numbers.Real subclass and numpy bools are handled in
        # _as_price_float, which rejects both (in any position) rather than
        # coercing True/False to 1.0/0.0 — never a meaningful price.
        return _floats(series, _as_price_float, field)
    if hasattr(first, field):
        return _floats(series, lambda b: getattr(b, field), field)
    if isinstance(first, dict) and field in first:
        return _floats(series, lambda b: b[field], field)
    raise TypeError(f"cannot extract {field!r} from {type(first).__name__}")


def _as_price_float(value) -> float:
    """Convert a single price element to ``float``, rejecting bool.

    Rejects both Python ``bool`` and numpy ``np.bool_`` (neither is a
    meaningful price) while accepting Python and numpy numeric scalars.
    """
    if isinstance(value, (bool, np.bool_)):
        raise TypeError("indicator input must be numeric, got bool")
    return float(value)


def _floats(series, getter: Callable, field: str) -> pd.Series:
    """Apply ``getter`` to every element and build a float Series.

    Any per-element failure (a malformed element after the first, a missing
    attribute/key, a non-numeric value) is normalised to ``TypeError`` so the
    whole indicator API only ever raises ``TypeError`` for bad input.
    """
    try:
        return pd.Series([float(getter(b)) for b in series], dtype=float)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise TypeError(f"indicator input element is not a valid {field!r} value: {exc}") from exc


def sma(series: pd.Series, period: int) -> pd.Series:
    """Simple Moving Average."""
    series = _coerce_series(series)
    return series.rolling(window=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential Moving Average."""
    series = _coerce_series(series)
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index (0–100)."""
    series = _coerce_series(series)
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    result = 100 - (100 / (1 + rs))
    # When avg_loss is zero (sustained uptrend) RS is infinite → RSI = 100.
    # pandas 3.x requires a Series here (rejects raw ndarray), so wrap the
    # np.where result with the same index as ``result``.
    fill_values = pd.Series(np.where(avg_loss == 0, 100.0, np.nan), index=result.index)
    result = result.fillna(fill_values)
    return result


def macd(
    series: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """MACD line, signal line, histogram."""
    series = _coerce_series(series)
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
    series = _coerce_series(series)
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
    high = _coerce_series(high, "high")
    low = _coerce_series(low, "low")
    close = _coerce_series(close, "close")
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
    high = _coerce_series(high, "high")
    low = _coerce_series(low, "low")
    close = _coerce_series(close, "close")
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
    high = _coerce_series(high, "high")
    low = _coerce_series(low, "low")
    close = _coerce_series(close, "close")
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
    high = _coerce_series(high, "high")
    low = _coerce_series(low, "low")
    close = _coerce_series(close, "close")
    volume = _coerce_series(volume, "volume")
    typical_price = (high + low + close) / 3
    cum_tp_vol = (typical_price * volume).cumsum()
    cum_vol = volume.cumsum().replace(0, np.nan)
    return cum_tp_vol / cum_vol


def donchian_channels(
    high: pd.Series,
    low: pd.Series,
    period: int = 20,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Donchian channel: upper (rolling-max high), middle (midpoint), lower (rolling-min low)."""
    high = _coerce_series(high, "high")
    low = _coerce_series(low, "low")
    upper = high.rolling(window=period).max()
    lower = low.rolling(window=period).min()
    middle = (upper + lower) / 2
    return upper, middle, lower


def keltner_channels(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 20,
    atr_period: int = 10,
    multiplier: float = 2.0,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Keltner channel: EMA(close) basis ± multiplier × ATR(atr_period)."""
    high = _coerce_series(high, "high")
    low = _coerce_series(low, "low")
    close = _coerce_series(close, "close")
    middle = ema(close, period)
    atr_series = atr(high, low, close, atr_period)
    upper = middle + multiplier * atr_series
    lower = middle - multiplier * atr_series
    return upper, middle, lower


def obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    """On-Balance Volume: cumulative volume signed by the close-to-close direction."""
    close = _coerce_series(close, "close")
    volume = _coerce_series(volume, "volume")
    direction = np.sign(close.diff()).fillna(0.0)
    return (direction * volume).cumsum()


def mfi(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    volume: pd.Series,
    period: int = 14,
) -> pd.Series:
    """Money Flow Index (0–100): volume-weighted RSI of typical price."""
    high = _coerce_series(high, "high")
    low = _coerce_series(low, "low")
    close = _coerce_series(close, "close")
    volume = _coerce_series(volume, "volume")
    tp = (high + low + close) / 3
    raw_money_flow = tp * volume
    tp_diff = tp.diff()
    pos_flow = raw_money_flow.where(tp_diff > 0, 0.0)
    neg_flow = raw_money_flow.where(tp_diff < 0, 0.0)
    pos_sum = pos_flow.rolling(window=period).sum()
    neg_sum = neg_flow.rolling(window=period).sum()
    ratio = pos_sum / neg_sum.replace(0, np.nan)
    result = 100 - (100 / (1 + ratio))
    # neg_sum == 0 over a window (no down moves) → MFI = 100, matching rsi().
    fill_values = pd.Series(np.where(neg_sum == 0, 100.0, np.nan), index=result.index)
    return result.fillna(fill_values)


def roc(series: pd.Series, period: int = 12) -> pd.Series:
    """Rate of Change (percent) over ``period`` bars."""
    series = _coerce_series(series)
    prev = series.shift(period)
    return (series - prev) / prev.replace(0, np.nan) * 100


def cci(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 20,
) -> pd.Series:
    """Commodity Channel Index: typical-price deviation scaled by 0.015 × mean deviation."""
    high = _coerce_series(high, "high")
    low = _coerce_series(low, "low")
    close = _coerce_series(close, "close")
    tp = (high + low + close) / 3
    sma_tp = tp.rolling(window=period).mean()
    mean_dev = tp.rolling(window=period).apply(
        lambda window: np.abs(window - window.mean()).mean(), raw=True
    )
    return (tp - sma_tp) / (0.015 * mean_dev.replace(0, np.nan))


def williams_r(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> pd.Series:
    """Williams %R (−100–0): close position within the trailing high/low range."""
    high = _coerce_series(high, "high")
    low = _coerce_series(low, "low")
    close = _coerce_series(close, "close")
    highest_high = high.rolling(window=period).max()
    lowest_low = low.rolling(window=period).min()
    denom = (highest_high - lowest_low).replace(0, np.nan)
    return -100 * (highest_high - close) / denom


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
        "donchian_channels": IndicatorSpec(
            helper=donchian_channels,
            data_inputs=("high", "low"),
            kwarg_names=("period",),
            tuple_arity=3,
        ),
        "keltner_channels": IndicatorSpec(
            helper=keltner_channels,
            data_inputs=("high", "low", "close"),
            kwarg_names=("period", "atr_period", "multiplier"),
            tuple_arity=3,
            float_kwargs=frozenset({"multiplier"}),
        ),
        "obv": IndicatorSpec(
            helper=obv,
            data_inputs=("close", "volume"),
            kwarg_names=(),
            tuple_arity=None,
        ),
        "mfi": IndicatorSpec(
            helper=mfi,
            data_inputs=("high", "low", "close", "volume"),
            kwarg_names=("period",),
            tuple_arity=None,
        ),
        "roc": IndicatorSpec(
            helper=roc, data_inputs=("series",), kwarg_names=("period",), tuple_arity=None
        ),
        "cci": IndicatorSpec(
            helper=cci,
            data_inputs=("high", "low", "close"),
            kwarg_names=("period",),
            tuple_arity=None,
        ),
        "williams_r": IndicatorSpec(
            helper=williams_r,
            data_inputs=("high", "low", "close"),
            kwarg_names=("period",),
            tuple_arity=None,
        ),
    }
)
