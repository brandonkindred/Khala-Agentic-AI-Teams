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

import numbers
from typing import Optional, Sequence

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


# ---------------------------------------------------------------------------
# Unified accessor — backs ``ctx.indicator(...)`` (issue #703).
#
# A single prescriptive entry point that resolves a DSL indicator name + params
# to its latest scalar value, routed through the same ``_impl`` Series helpers
# the engine's predicate evaluator uses, so a value read here is identical to
# the engine's per-bar value for the same bar sequence. Unlike the warm-up
# convention of the per-indicator helpers above (which return ``0.0``), this
# accessor returns ``None`` during warm-up so callers can distinguish "no value
# yet" from a genuine ``0.0`` reading.
# ---------------------------------------------------------------------------

# DSL indicator names this accessor understands (mirrors spec_dsl.IndicatorName);
# kept as a literal set so the flat sandbox copy needs no spec_dsl import.
_VALID_INDICATORS: frozenset[str] = frozenset(
    {"sma", "ema", "rsi", "macd", "bollinger", "atr", "adx", "stochastic", "vwap"}
)
_VALID_SOURCES: frozenset[str] = frozenset(
    {"close", "open", "high", "low", "volume", "hl2", "ohlc4"}
)

# Allowed param keys per indicator (mirrors spec_dsl's _INDICATOR_PARAM_SPECS:
# required ∪ optional, excluding ``source`` which is a dedicated argument). Kept
# literal so the flat sandbox copy needs no spec_dsl import. Used to reject an
# unexpected/typo'd kwarg (e.g. ``perod=14``) rather than silently trading on
# defaults — matching IndicatorRef's strictness for reads the static gate cannot
# see through (e.g. ``**kwargs`` unpacking).
_INDICATOR_PARAM_KEYS: dict[str, frozenset[str]] = {
    "sma": frozenset({"period"}),
    "ema": frozenset({"period"}),
    "rsi": frozenset({"period"}),
    "macd": frozenset({"fast", "slow", "signal", "output"}),
    "bollinger": frozenset({"period", "num_std", "band"}),
    "atr": frozenset({"period"}),
    "adx": frozenset({"period"}),
    "stochastic": frozenset({"k_period", "d_period", "output"}),
    "vwap": frozenset(),
}


def _last_or_none(series: pd.Series) -> Optional[float]:
    """Latest finite value of ``series``, or ``None`` when warm-up/empty.

    Preconditions:
        ``series`` is a ``pd.Series`` (as returned by an ``_impl`` helper).
    Postconditions:
        Returns ``float`` of the last element, or ``None`` when the series is
        empty or its last value is ``None``/``NaN`` — the warm-up signal.
    """
    if series is None or len(series) == 0:
        return None
    val = series.iloc[-1]
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return None
    return float(val)


def _source_values(history: Sequence, source: str) -> list:
    """Project ``history`` onto a single price series per ``source``.

    Preconditions:
        ``source`` is one of :data:`_VALID_SOURCES`; elements of ``history``
        are bar-like objects exposing OHLCV attributes (or plain numbers,
        returned verbatim).
    Postconditions:
        Returns a ``list[float]`` aligned with ``history`` — the input the
        single-series ``_impl`` helpers consume (matching the engine's
        ``select_source_series`` projection).
    """
    if source not in _VALID_SOURCES:
        raise ValueError(f"unknown indicator source {source!r}; allowed: {sorted(_VALID_SOURCES)}")
    out: list = []
    for b in history:
        if isinstance(b, numbers.Real) and not isinstance(b, bool):
            out.append(float(b))
        elif source == "hl2":
            out.append((float(b.high) + float(b.low)) / 2.0)
        elif source == "ohlc4":
            out.append((float(b.open) + float(b.high) + float(b.low) + float(b.close)) / 4.0)
        else:
            out.append(float(getattr(b, source)))
    return out


def _select(series_by_key: dict, key: str, indicator: str) -> pd.Series:
    """Pick a tuple-valued indicator's output series by its selector value."""
    series = series_by_key.get(key)
    if series is None:
        raise ValueError(
            f"indicator {indicator!r} got invalid selector {key!r}; "
            f"allowed: {sorted(series_by_key)}"
        )
    return series


def indicator_value(
    name: str,
    history: Sequence,
    *,
    source: str = "close",
    **params,
) -> Optional[float]:
    """Latest scalar value of DSL indicator ``name`` over ``history``.

    The single source of truth behind ``ctx.indicator(...)`` for both the
    streaming sandbox (``StrategyContext``) and the predicate-conformance
    shadow (``_ShadowContext``). Routes through the same ``_impl`` Series
    helpers the engine uses, so the returned value equals the engine's
    per-bar value for the same bars.

    Preconditions:
        ``name`` is a known DSL indicator (:data:`_VALID_INDICATORS`);
        ``sma``/``ema`` require a ``period`` param; selector params
        (``macd.output``, ``bollinger.band``, ``stochastic.output``) and
        ``source`` are valid for the indicator. Contract violations raise
        ``ValueError`` (a caller bug) — they are never silently coerced.
    Postconditions:
        Returns the latest indicator value as ``float``, or ``None`` when
        ``history`` is empty or the indicator is still in warm-up.
    """
    if name not in _VALID_INDICATORS:
        raise ValueError(f"unknown indicator {name!r}; allowed: {sorted(_VALID_INDICATORS)}")
    # Reject unexpected/typo'd param keys up front (independent of warm-up) so a
    # mis-parameterized read raises rather than silently trading on defaults.
    unexpected = set(params) - _INDICATOR_PARAM_KEYS[name]
    if unexpected:
        raise ValueError(
            f"indicator {name!r} got unexpected param(s) {sorted(unexpected)}; "
            f"allowed: {sorted(_INDICATOR_PARAM_KEYS[name])}"
        )
    if not history:
        return None

    if name in ("sma", "ema"):
        if "period" not in params:
            raise ValueError(f"indicator {name!r} requires a 'period' param")
        data = _source_values(history, source)
        fn = _impl.sma if name == "sma" else _impl.ema
        return _last_or_none(fn(data, int(params["period"])))

    if name == "rsi":
        data = _source_values(history, source)
        return _last_or_none(_impl.rsi(data, int(params.get("period", 14))))

    if name == "macd":
        data = _source_values(history, source)
        macd_line, signal_line, hist = _impl.macd(
            data,
            fast=int(params.get("fast", 12)),
            slow=int(params.get("slow", 26)),
            signal=int(params.get("signal", 9)),
        )
        chosen = _select(
            {"macd": macd_line, "signal": signal_line, "histogram": hist},
            str(params.get("output", "macd")),
            name,
        )
        return _last_or_none(chosen)

    if name == "bollinger":
        data = _source_values(history, source)
        upper, middle, lower = _impl.bollinger_bands(
            data,
            period=int(params.get("period", 20)),
            num_std=float(params.get("num_std", 2.0)),
        )
        chosen = _select(
            {"upper": upper, "middle": middle, "lower": lower},
            str(params.get("band", "middle")),
            name,
        )
        return _last_or_none(chosen)

    # OHLC-sourced indicators read their fields directly and forbid a `source`
    # override (mirrors spec_dsl's allow_source=False for these names); each
    # `_impl` arg slot extracts its own field from the bar sequence. Reject a
    # non-default source rather than silently computing a different indicator
    # than the caller requested — otherwise check #1 would credit the read by
    # name and the strategy would run mis-sourced instead of being refined.
    if source != "close":
        raise ValueError(f"indicator {name!r} does not accept a 'source' override")
    if name == "atr":
        return _last_or_none(
            _impl.atr(history, history, history, period=int(params.get("period", 14)))
        )
    if name == "adx":
        return _last_or_none(
            _impl.adx(history, history, history, period=int(params.get("period", 14)))
        )
    if name == "stochastic":
        pct_k, pct_d = _impl.stochastic(
            history,
            history,
            history,
            k_period=int(params.get("k_period", 14)),
            d_period=int(params.get("d_period", 3)),
        )
        chosen = _select({"k": pct_k, "d": pct_d}, str(params.get("output", "k")), name)
        return _last_or_none(chosen)

    # name == "vwap" (only remaining valid name)
    return _last_or_none(_impl.vwap(history, history, history, history))
