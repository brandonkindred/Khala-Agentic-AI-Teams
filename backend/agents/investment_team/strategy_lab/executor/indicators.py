"""Pre-built technical indicators derived from the streaming IndicatorRegistry.

This module is the vectorized (``pd.Series``-returning) face of the single
canonical indicator math in :mod:`investment_team.strategy_lab.indicators.streaming`.
Every function here builds registry-ready bar objects from the caller's price
series and walks **one** :class:`IndicatorRegistry` over the expanding history,
collecting the trailing scalar per bar into a ``pd.Series`` — so a value here is
byte-identical, by construction, to the value ``StreamingHistoryView`` produces
at the same bar. There is exactly one implementation of each indicator's math
(the registry); this module holds none of its own.

This module is copied into the strategy sandbox as ``_indicators_impl.py`` (see
``trading_service.strategy.streaming_harness.StreamingHarness``) — the sandbox's
real ``indicators.py`` is a copy of ``strategy_indicators.py``'s scalar contract,
which is what ``from indicators import <function_name>`` resolves to at runtime.
This module is instead consulted in-process by the static coverage probe
(``coverage_probe/indicator_probe.py``) via the ``INDICATORS`` registry, and by
``market_regime.py`` for its Series-returning ``sma``/``adx``/``atr`` inputs.

Every function accepts either a ``pd.Series`` or a sequence the strategy already
has on hand — a ``list[float]`` or the ``list[Bar]`` that ``ctx.history(symbol,
n)`` returns — and returns ``pd.Series`` or a tuple of ``pd.Series``.  Inputs are
coerced at the boundary (see ``_coerce_series``), so callers do not need to wrap
price data in ``pd.Series`` themselves.  Warm-up bars — those where the registry
has no value yet — are NaN; callers should skip them.

Contract note: OHLC(V) inputs are zipped positionally and truncated to the
shortest, so callers must pass equal-length, index-aligned series.  Every real
caller (the coverage probe's DataFrame columns and ``market_regime``'s repeated
``bars`` argument) satisfies this.
"""

from __future__ import annotations

import numbers
from collections import deque
from dataclasses import dataclass
from types import MappingProxyType
from typing import Callable, List, Literal, Mapping, Optional, Sequence, Tuple, Union

import numpy as np
import pandas as pd

# The streaming ``IndicatorRegistry`` is the engine's authoritative indicator
# math (``StreamingHistoryView``). Deriving these Series helpers from it makes a
# value read here byte-identical to the engine's per-bar reads. The flat sandbox
# harness copies ``streaming.py`` alongside as ``_streaming_indicators.py`` (see
# ``strategy_indicators.py``'s identical fallback), so the import must resolve in
# both the in-package and flat-sandbox layouts.
try:  # in-package use (coverage probe, market_regime, in-process tests)
    from ..indicators.streaming import IndicatorRegistry
except ImportError:  # flat sandbox layout
    from _streaming_indicators import IndicatorRegistry  # type: ignore[no-redef]


NAN = float("nan")


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


# ---------------------------------------------------------------------------
# Registry-derived scaffolding.
#
# The Series helpers below hold no indicator math: each builds registry-ready
# bars from its coerced inputs and walks one ``IndicatorRegistry`` over the
# expanding history, collecting the trailing scalar per bar. Appending to a
# single growing ``window`` list preserves object identity at index ``-2`` on
# every step, so ``IndicatorRegistry._advance_kind`` classifies the advance as
# ``"expand"`` and updates incrementally — the walk is O(n·window) per
# indicator, not O(n²·window), and every value equals the runtime's per-bar
# read by construction.
# ---------------------------------------------------------------------------


class _Bar:
    """Minimal bar exposing the OHLCV attributes the registry reads.

    ``symbol``/``timestamp`` are intentionally absent — the registry reads them
    via ``_safe_getattr`` (degrading to ``None``), and a constant ``None`` keeps
    the symbol-scoped cache keys (macd/donchian/keltner/obv/mfi/roc/cci/
    williams_r) stable across the single-stream expanding-prefix walk.

    Invariant: every field is a finite float; omitted fields default to ``0.0``.
    """

    __slots__ = ("open", "high", "low", "close", "volume")

    def __init__(
        self,
        *,
        open: float = 0.0,
        high: float = 0.0,
        low: float = 0.0,
        close: float = 0.0,
        volume: float = 0.0,
    ) -> None:
        self.open = open
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume


def _close_bars(s: pd.Series) -> List[_Bar]:
    """Project a single source series onto close-only registry bars."""
    return [_Bar(close=float(v)) for v in s]


def _hl_bars(h: pd.Series, low: pd.Series) -> List[_Bar]:
    """Zip high/low series into registry bars (donchian_channels)."""
    return [_Bar(high=float(a), low=float(b)) for a, b in zip(h, low)]


def _hlc_bars(h: pd.Series, low: pd.Series, c: pd.Series) -> List[_Bar]:
    """Zip high/low/close series into registry bars (atr/adx/stochastic/…)."""
    return [_Bar(high=float(a), low=float(b), close=float(d)) for a, b, d in zip(h, low, c)]


def _hlcv_bars(h: pd.Series, low: pd.Series, c: pd.Series, v: pd.Series) -> List[_Bar]:
    """Zip high/low/close/volume series into registry bars (vwap/mfi)."""
    return [
        _Bar(high=float(a), low=float(b), close=float(d), volume=float(e))
        for a, b, d, e in zip(h, low, c, v)
    ]


def _cv_bars(c: pd.Series, v: pd.Series) -> List[_Bar]:
    """Zip close/volume series into registry bars (obv)."""
    return [_Bar(close=float(a), volume=float(b)) for a, b in zip(c, v)]


def _emit(scalars: Sequence[Optional[float]], index) -> pd.Series:
    """Build a float Series from per-bar registry scalars, ``None`` → NaN.

    Preconditions: ``len(scalars) == len(index)``.
    Postconditions: a float ``pd.Series`` on ``index`` whose warm-up slots
    (``None``) are NaN.
    """
    return pd.Series([NAN if v is None else v for v in scalars], index=index, dtype=float)


def _run_single(
    bars: Sequence[_Bar],
    index,
    fn: Callable[[IndicatorRegistry, List[_Bar]], Optional[float]],
) -> pd.Series:
    """Walk one registry over the expanding ``bars`` history for a scalar indicator.

    Preconditions: ``len(bars) == len(index)``; ``fn`` maps ``(registry, window)``
    to the trailing-bar scalar (or ``None`` during warm-up).
    Postconditions: a float ``pd.Series`` on ``index`` equal, bar-for-bar, to the
    runtime's per-bar reads.
    """
    reg = IndicatorRegistry()
    window: List[_Bar] = []
    out: List[Optional[float]] = []
    for b in bars:
        window.append(b)
        out.append(fn(reg, window))
    return _emit(out, index)


def _run_tuple(
    bars: Sequence[_Bar],
    index,
    fns: Sequence[Callable[[IndicatorRegistry, List[_Bar]], Optional[float]]],
    max_bars: Optional[int] = None,
) -> Tuple[pd.Series, ...]:
    """Walk one registry for a multi-output indicator, one column per ``fns`` entry.

    Preconditions: ``len(bars) == len(index)``; each ``fn`` selects one output of
    the same underlying indicator (the registry caches the full tuple on the
    same-bar fingerprint, so the 2nd/3rd select of a bar is a cache hit).
    ``max_bars``, when set, bounds the history handed to the registry to the
    trailing ``max_bars`` bars — mirroring ``StreamingHistoryView`` /
    ``compute_indicator_series`` (``deque(maxlen=_SERIES_WINDOW)`` + ``list(...)``)
    so a history-length-dependent indicator (MACD's signal EMA spans the whole
    macd_line) stays bit-identical to the engine past the cap. Fixed-window
    indicators are unaffected (they only read the trailing ``period`` bars), so
    the default is unbounded.
    Postconditions: a tuple of float ``pd.Series`` on ``index``, one per ``fns``
    entry in declared order.
    """
    reg = IndicatorRegistry()
    # Unbounded: reuse one growing list (object identity at index -2 lets the
    # registry classify each step as "expand"). Bounded: a maxlen deque, and each
    # bar hands the registry a fresh ``list(window)`` snapshot — the same shape
    # ``compute_indicator_series`` feeds, so once the window fills the registry
    # sees a "slide" and the two stay bit-identical.
    window: "deque[_Bar] | List[_Bar]" = deque(maxlen=max_bars) if max_bars else []
    cols: List[List[Optional[float]]] = [[] for _ in fns]
    for b in bars:
        window.append(b)
        snapshot: List[_Bar] = list(window) if max_bars else window  # type: ignore[assignment]
        for i, fn in enumerate(fns):
            cols[i].append(fn(reg, snapshot))
    return tuple(_emit(col, index) for col in cols)


# ---------------------------------------------------------------------------
# The 16 indicators — each a thin vectorization of an ``IndicatorRegistry``
# method. Signatures and return shapes match the historical pandas API.
# ---------------------------------------------------------------------------


def sma(series: pd.Series, period: int) -> pd.Series:
    """Simple Moving Average.

    Preconditions: ``series`` is coercible; ``period >= 1``.
    Postconditions: a same-length Series (caller's index), NaN until ``period``
    bars exist, then ``IndicatorRegistry.sma`` at each bar.
    """
    s = _coerce_series(series)
    return _run_single(_close_bars(s), s.index, lambda r, w: r.sma(w, int(period), source="close"))


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential Moving Average (windowed EMA — the runtime convention).

    Preconditions: ``series`` is coercible; ``period >= 1``.
    Postconditions: a same-length Series, NaN until ``period`` bars exist, then
    ``IndicatorRegistry.ema`` (the trailing-window EMA reseeded from the oldest
    in-window bar) at each bar.
    """
    s = _coerce_series(series)
    return _run_single(_close_bars(s), s.index, lambda r, w: r.ema(w, int(period), source="close"))


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index (0–100), simple-mean smoothing (runtime convention).

    Preconditions: ``series`` is coercible; ``period >= 1``.
    Postconditions: a same-length Series, NaN until ``period + 1`` bars exist,
    then ``IndicatorRegistry.rsi`` at each bar (100 on a sustained up-window with
    no losses, 50 on a flat window).
    """
    s = _coerce_series(series)
    return _run_single(_close_bars(s), s.index, lambda r, w: r.rsi(w, int(period), source="close"))


def macd(
    series: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """MACD line, signal line, histogram (windowed-EMA basis — runtime convention).

    Preconditions: ``series`` is coercible; ``2 <= fast < slow``; ``signal >= 2``.
    Postconditions: three same-length Series; the line is NaN until ``slow`` bars
    exist, the signal and histogram until ``slow + signal - 1``.

    Unlike the fixed-window indicators, MACD's signal EMA folds over the entire
    macd_line, so its value depends on the full history length. The walk is
    therefore bounded to the engine's trailing-history window
    (``STREAMING_WINDOW_BARS``) so this reference stays bit-identical to
    ``StreamingHistoryView`` / ``compute_indicator_series`` for histories longer
    than the cap — otherwise the coverage probe (which resolves MACD through this
    helper) would score MACD predicates differently from the engine it models.
    """
    # Lazy import (mirrors :func:`_windowed_obv`): only reached in-package
    # (coverage probe / tests), never in the flat sandbox where MACD's math runs
    # through the scalar ``strategy_indicators`` API instead.
    from ..runtime_window import STREAMING_WINDOW_BARS

    s = _coerce_series(series)
    f, sl, sg = int(fast), int(slow), int(signal)
    return _run_tuple(
        _close_bars(s),
        s.index,
        [
            lambda r, w: r.macd(w, f, sl, sg, source="close", select="macd"),
            lambda r, w: r.macd(w, f, sl, sg, source="close", select="signal"),
            lambda r, w: r.macd(w, f, sl, sg, source="close", select="histogram"),
        ],
        max_bars=STREAMING_WINDOW_BARS,
    )


def bollinger_bands(
    series: pd.Series,
    period: int = 20,
    num_std: float = 2.0,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Upper band, middle band (SMA), lower band (population std — runtime convention).

    Preconditions: ``series`` is coercible; ``period >= 1``; ``num_std > 0``.
    Postconditions: three same-length Series, NaN until ``period`` bars exist,
    then ``middle ± num_std × population_std`` around the trailing SMA.
    """
    s = _coerce_series(series)
    p, k = int(period), float(num_std)
    return _run_tuple(
        _close_bars(s),
        s.index,
        [
            lambda r, w: r.bollinger_bands(w, p, k, source="close", select="upper"),
            lambda r, w: r.bollinger_bands(w, p, k, source="close", select="middle"),
            lambda r, w: r.bollinger_bands(w, p, k, source="close", select="lower"),
        ],
    )


def atr(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> pd.Series:
    """Average True Range (simple average of true range).

    Preconditions: ``high``/``low``/``close`` are coercible, index-aligned OHLC
    series; ``period >= 1``.
    Postconditions: a same-length Series, NaN until ``period + 1`` bars exist,
    then ``IndicatorRegistry.atr`` at each bar.
    """
    h = _coerce_series(high, "high")
    low = _coerce_series(low, "low")
    c = _coerce_series(close, "close")
    bars = _hlc_bars(h, low, c)
    return _run_single(bars, h.index[: len(bars)], lambda r, w: r.atr(w, int(period)))


def adx(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> pd.Series:
    """Average Directional Index (0–100), un-smoothed single-DX (runtime convention).

    Preconditions: ``high``/``low``/``close`` are coercible, index-aligned OHLC
    series; ``period >= 1``.
    Postconditions: a same-length Series, NaN until ``2 × period + 1`` bars exist,
    then ``IndicatorRegistry.adx`` at each bar.
    """
    h = _coerce_series(high, "high")
    low = _coerce_series(low, "low")
    c = _coerce_series(close, "close")
    bars = _hlc_bars(h, low, c)
    return _run_single(bars, h.index[: len(bars)], lambda r, w: r.adx(w, int(period)))


def stochastic(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    k_period: int = 14,
    d_period: int = 3,
) -> tuple[pd.Series, pd.Series]:
    """Stochastic Oscillator (%K, %D).

    Preconditions: ``high``/``low``/``close`` are coercible, index-aligned OHLC
    series; ``k_period >= 1``; ``d_period >= 1``.
    Postconditions: two same-length Series; %K is NaN until ``k_period`` bars
    exist, %D until ``k_period + d_period - 1`` (50 neutral on a flat window).
    """
    h = _coerce_series(high, "high")
    low = _coerce_series(low, "low")
    c = _coerce_series(close, "close")
    kp, dp = int(k_period), int(d_period)
    bars = _hlc_bars(h, low, c)
    return _run_tuple(
        bars,
        h.index[: len(bars)],
        [
            lambda r, w: r.stochastic(w, kp, dp, select="k"),
            lambda r, w: r.stochastic(w, kp, dp, select="d"),
        ],
    )


def vwap(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    volume: pd.Series,
) -> pd.Series:
    """Cumulative Volume Weighted Average Price (no intraday reset).

    Preconditions: ``high``/``low``/``close``/``volume`` are coercible,
    index-aligned series.
    Postconditions: a same-length Series; ``Σ(typical·volume) / Σ volume`` over
    all bars so far (``typical = (high+low+close)/3``), falling back to the
    running mean close when cumulative volume is 0 — the runtime convention
    (``IndicatorRegistry.vwap`` with ``period=None``).
    """
    h = _coerce_series(high, "high")
    low = _coerce_series(low, "low")
    c = _coerce_series(close, "close")
    v = _coerce_series(volume, "volume")
    bars = _hlcv_bars(h, low, c, v)
    return _run_single(bars, h.index[: len(bars)], lambda r, w: r.vwap(w, period=None))


def donchian_channels(
    high: pd.Series,
    low: pd.Series,
    period: int = 20,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Donchian channel: upper (rolling-max high), middle (midpoint), lower (rolling-min low).

    Preconditions: ``high``/``low`` are coercible, index-aligned OHLC series;
    ``period >= 1``.
    Postconditions: three same-length Series, NaN for the first ``period - 1``
    rows, then the trailing-``period`` high/low extrema and their midpoint.
    """
    h = _coerce_series(high, "high")
    low = _coerce_series(low, "low")
    p = int(period)
    bars = _hl_bars(h, low)
    return _run_tuple(
        bars,
        h.index[: len(bars)],
        [
            lambda r, w: r.donchian(w, p, select="upper"),
            lambda r, w: r.donchian(w, p, select="middle"),
            lambda r, w: r.donchian(w, p, select="lower"),
        ],
    )


def keltner_channels(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 20,
    atr_period: int = 10,
    multiplier: float = 2.0,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Keltner channel: windowed-EMA(close) basis ± multiplier × ATR(atr_period).

    Preconditions: ``high``/``low``/``close`` are coercible, index-aligned OHLC
    series; ``period >= 1``; ``atr_period >= 1``.
    Postconditions: three same-length Series, NaN until ``max(period, atr_period
    + 1)`` bars exist, then ``middle ± multiplier × ATR(atr_period)`` around the
    windowed close-EMA — ``IndicatorRegistry.keltner`` at each bar.
    """
    h = _coerce_series(high, "high")
    low = _coerce_series(low, "low")
    c = _coerce_series(close, "close")
    p, ap, mult = int(period), int(atr_period), float(multiplier)
    bars = _hlc_bars(h, low, c)
    return _run_tuple(
        bars,
        h.index[: len(bars)],
        [
            lambda r, w: r.keltner(w, p, ap, mult, select="upper"),
            lambda r, w: r.keltner(w, p, ap, mult, select="middle"),
            lambda r, w: r.keltner(w, p, ap, mult, select="lower"),
        ],
    )


def obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    """On-Balance Volume: cumulative volume signed by the close-to-close direction.

    Preconditions: ``close``/``volume`` are coercible, index-aligned series.
    Postconditions: a same-length cumulative Series; each step adds ``volume`` on
    an up-close, subtracts it on a down-close, is unchanged on a flat close (the
    first bar contributes 0, having no prior close).
    """
    c = _coerce_series(close, "close")
    v = _coerce_series(volume, "volume")
    bars = _cv_bars(c, v)
    return _run_single(bars, c.index[: len(bars)], lambda r, w: r.obv(w))


def mfi(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    volume: pd.Series,
    period: int = 14,
) -> pd.Series:
    """Money Flow Index (0–100): volume-weighted RSI of typical price.

    Preconditions: ``high``/``low``/``close``/``volume`` are coercible,
    index-aligned series; ``period >= 1``.
    Postconditions: a same-length Series in ``[0, 100]``, NaN until ``period + 1``
    bars exist; a full window with no down-flow yields 100 (50 when there is no
    flow at all) — ``IndicatorRegistry.mfi`` at each bar.
    """
    h = _coerce_series(high, "high")
    low = _coerce_series(low, "low")
    c = _coerce_series(close, "close")
    v = _coerce_series(volume, "volume")
    bars = _hlcv_bars(h, low, c, v)
    return _run_single(bars, h.index[: len(bars)], lambda r, w: r.mfi(w, int(period)))


def roc(series: pd.Series, period: int = 12) -> pd.Series:
    """Rate of Change (percent) over ``period`` bars.

    Preconditions: ``series`` is coercible; ``period >= 1``.
    Postconditions: a same-length Series, NaN until ``period + 1`` bars exist,
    then ``100 × (price − price[−period]) / price[−period]`` (0.0 when the
    reference price is exactly 0) — ``IndicatorRegistry.roc`` at each bar.
    """
    s = _coerce_series(series)
    return _run_single(_close_bars(s), s.index, lambda r, w: r.roc(w, int(period), source="close"))


def cci(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 20,
) -> pd.Series:
    """Commodity Channel Index: typical-price deviation scaled by 0.015 × mean deviation.

    Preconditions: ``high``/``low``/``close`` are coercible, index-aligned OHLC
    series; ``period >= 1``.
    Postconditions: a same-length Series, NaN for the first ``period - 1`` rows,
    then ``(tp − sma_tp) / (0.015 × mean_deviation)`` (0.0 on a flat window) —
    ``IndicatorRegistry.cci`` at each bar.
    """
    h = _coerce_series(high, "high")
    low = _coerce_series(low, "low")
    c = _coerce_series(close, "close")
    bars = _hlc_bars(h, low, c)
    return _run_single(bars, h.index[: len(bars)], lambda r, w: r.cci(w, int(period)))


def williams_r(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> pd.Series:
    """Williams %R (−100–0): close position within the trailing high/low range.

    Preconditions: ``high``/``low``/``close`` are coercible, index-aligned OHLC
    series; ``period >= 1``.
    Postconditions: a same-length Series in ``[−100, 0]``, NaN for the first
    ``period - 1`` rows, then ``−100 × (highest_high − close) / range`` (−50.0
    neutral on a flat window) — ``IndicatorRegistry.williams_r`` at each bar.
    """
    h = _coerce_series(high, "high")
    low = _coerce_series(low, "low")
    c = _coerce_series(close, "close")
    bars = _hlc_bars(h, low, c)
    return _run_single(bars, h.index[: len(bars)], lambda r, w: r.williams_r(w, int(period)))


# ---------------------------------------------------------------------------
# Coverage-probe cumulative wrappers.
#
# ``vwap``/``obv`` are the two indicators whose full-history running total
# diverges from what the runtime's bounded ``StreamingHistoryView`` trades on,
# so the coverage-probe reference (``INDICATORS``) routes them through these
# window-bounded wrappers instead of the unbounded functions above.
# ---------------------------------------------------------------------------


def _rebase_cumulative(cumulative: pd.Series, lag: int) -> pd.Series:
    """Re-base a cumulative series to a trailing window by subtracting its lagged self.

    Preconditions: ``cumulative`` is a running sum (monotone in the accumulated
    sign); ``lag >= 1``.
    Postconditions: ``cumulative[t] - cumulative[t - lag]`` (the pre-window prefix
    treated as 0 during warm-up), i.e. the sum accrued over the trailing ``lag``
    steps.
    """
    return cumulative - cumulative.shift(lag).fillna(0.0)


def _windowed_obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    """OBV re-based to the trailing ``STREAMING_WINDOW_BARS`` window (coverage probe only).

    Preconditions: ``close``/``volume`` are coercible series of equal length.
    Postconditions: a same-length series matching the runtime's windowed OBV bar
    for bar. The runtime computes OBV over a trailing window of
    ``STREAMING_WINDOW_BARS`` bars whose OLDEST bar has no in-window predecessor,
    so its close-to-close direction is undefined and it contributes 0 — i.e. the
    window carries ``STREAMING_WINDOW_BARS - 1`` signed terms. Hence the shift is
    ``window - 1``: ``full_obv[t] - full_obv[t - (window - 1)]`` sums exactly the
    signed volume of bars ``[t - window + 2 .. t]``. (Using ``window`` would
    over-count by the boundary bar's signed volume, since ``full_obv`` signs that
    bar against the bar just OUTSIDE the window.) The shifted term is 0 during
    warm-up, so the value equals full-history OBV until the window fills. The
    unbounded :func:`obv` grows without limit over long histories, so the probe
    would otherwise judge absolute-threshold OBV predicates on values the runtime
    (bounded ``StreamingHistoryView``) never sees.
    """
    # Imported lazily: this wrapper is only reached from the coverage probe (a
    # package context), never in the flat strategy sandbox where this module is
    # copied without its parent package.
    from ..runtime_window import STREAMING_WINDOW_BARS

    # Lag is ``window - 1`` (not ``window``): the oldest in-window bar has no
    # in-window predecessor, so its direction is undefined and it contributes 0.
    return _rebase_cumulative(obv(close, volume), STREAMING_WINDOW_BARS - 1)


def _windowed_vwap(
    high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series, period: int = 20
) -> pd.Series:
    """VWAP over a trailing ``period``-bar rolling window (coverage probe only).

    Preconditions: ``high``/``low``/``close``/``volume`` are coercible,
    index-aligned series; ``period >= 1``.
    Postconditions: a same-length Series matching the runtime's rolling VWAP bar
    for bar — this calls the very ``IndicatorRegistry.vwap(period=...)`` the
    engine calls, so the warm-up gate (NaN until ``period`` bars) and the
    zero-volume-window fallback (the window's mean close) are the runtime's, not
    a re-derivation. ``period`` defaults to 20 — the DSL's default VWAP window.
    """
    h = _coerce_series(high, "high")
    low = _coerce_series(low, "low")
    c = _coerce_series(close, "close")
    v = _coerce_series(volume, "volume")
    p = int(period)
    bars = _hlcv_bars(h, low, c, v)
    return _run_single(bars, h.index[: len(bars)], lambda r, w: r.vwap(w, period=p))


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
    # True for indicators (``vwap``, ``obv``) whose Series-returning reference
    # implementation above (:func:`vwap`, :func:`obv`) is an unbounded
    # full-history running total that diverges from what the runtime's bounded
    # ``StreamingHistoryView`` trades on. Their ``helper`` MUST therefore be a
    # window-bounded wrapper (``_windowed_*``), enforced by a registry guard
    # test. VWAP's window is now the DSL's own rolling ``period`` (unified with
    # the runtime); OBV's is still the harness's full retention ceiling — both
    # still need a ``_windowed_*`` wrapper rather than the unbounded reference.
    cumulative: bool = False


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
            # Windowed wrapper: the probe must judge VWAP predicates on the same
            # rolling-window value the runtime trades on (see :func:`_windowed_vwap`).
            helper=_windowed_vwap,
            data_inputs=("high", "low", "close", "volume"),
            kwarg_names=("period",),
            tuple_arity=None,
            cumulative=True,
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
            # Windowed wrapper, mirroring vwap: bound the cumulative to the
            # runtime's trailing window so the probe doesn't judge OBV predicates
            # on an unbounded full-history value (see :func:`_windowed_obv`).
            helper=_windowed_obv,
            data_inputs=("close", "volume"),
            kwarg_names=(),
            tuple_arity=None,
            cumulative=True,
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
