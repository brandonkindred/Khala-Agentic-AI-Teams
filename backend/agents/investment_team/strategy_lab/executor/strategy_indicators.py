"""Scalar-returning indicator API exposed to strategy ``on_bar`` code.

Strategy code does ``from indicators import ema, sma, ...`` and uses the
result directly in a comparison (``if ema(history, 20) > bar.close``), so each
helper here returns the **latest scalar value** of the underlying rolling
indicator — a ``float`` for single-output indicators, or a tuple of floats for
multi-output ones (``macd``, ``bollinger_bands``, ``stochastic``).

This is the single source of truth shared by the two execution paths that run
generated strategy code:

* the streaming sandbox copies this module in as ``indicators.py`` (alongside
  the Series-returning implementation as ``_indicators_impl.py`` and the
  registry as ``_streaming_indicators.py``), and
* the predicate-conformance shadow gate imports it in-process,

so a strategy sees one identical, scalar contract in both. Every helper — and
the ``ctx.indicator(...)`` accessor below — routes through the streaming
``IndicatorRegistry``, the same recurrences ``StreamingHistoryView`` runs per
bar, so a value read here is byte-identical to the engine's trailing value for
the same bars (no pandas/registry divergence).

Module invariant: every public helper returns the most recent registry value
(warm-up → ``0.0``); the accessor returns ``None`` during warm-up so callers can
distinguish "no value yet" from a genuine ``0.0``. None of them returns a
``pd.Series``.
"""

from __future__ import annotations

import math
import numbers
from typing import Optional, Sequence

try:  # in-package use (predicate-conformance gate, in-process tests)
    from . import indicators as _impl
except ImportError:  # flat sandbox layout: harness copies the impl as _indicators_impl.py
    import _indicators_impl as _impl  # type: ignore[no-redef]

# The streaming ``IndicatorRegistry`` is the engine's authoritative indicator
# math (``StreamingHistoryView``). Routing the scalar helpers and the unified
# accessor through it makes ``from indicators import sma`` and ``ctx.indicator``
# return byte-identical values to the engine's per-bar reads. The flat sandbox
# harness copies ``streaming.py`` alongside as ``_streaming_indicators.py``.
try:  # in-package use
    from ..indicators.streaming import IndicatorRegistry
except ImportError:  # flat sandbox layout
    from _streaming_indicators import IndicatorRegistry  # type: ignore[no-redef]


class _RegBar:
    """Minimal bar exposing the OHLCV attributes the registry reads.

    The registry computes against bar objects (``bar.close``/``bar.high``/…); we
    project the caller's price sequence(s) onto these so the scalar API and the
    accessor reuse the exact recurrence the engine view uses. ``timestamp`` /
    ``symbol`` are intentionally absent — the registry reads them via
    ``_safe_getattr`` (degrading to ``None``), and a single cold call per
    invocation needs no same-bar fingerprint.
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


def _value_bars(values) -> list:
    """Project a single source series onto close-only registry bars.

    ``values`` is any shape ``_coerce_series`` accepts (``pd.Series``,
    ``list[float]``, ``list[Bar]``, …); the projected scalar lands in ``close``
    so a registry call with ``source="close"`` reads exactly that series.
    """
    return [_RegBar(close=float(v)) for v in _impl._coerce_series(values)]


def _ohlc_bars(high, low, close, volume=None) -> list:
    """Zip separate OHLC(V) sequences into registry bars (atr/adx/stochastic/vwap)."""
    highs = [float(v) for v in _impl._coerce_series(high, "high")]
    lows = [float(v) for v in _impl._coerce_series(low, "low")]
    closes = [float(v) for v in _impl._coerce_series(close, "close")]
    vols = (
        [float(v) for v in _impl._coerce_series(volume, "volume")]
        if volume is not None
        else [0.0] * len(closes)
    )
    return [
        _RegBar(high=h, low=lo, close=c, volume=vol)
        for h, lo, c, vol in zip(highs, lows, closes, vols)
    ]


def _hl_bars(high, low) -> list:
    """Build registry bars from just high/low (Donchian). Other fields default to 0.0.

    Donchian reads only ``bar.high``/``bar.low``; constructing bars with only those
    fields set avoids feeding a placeholder into an OHLC slot the indicator never
    reads (vs. reusing ``_ohlc_bars`` with a dummy ``close``).
    """
    highs = [float(v) for v in _impl._coerce_series(high, "high")]
    lows = [float(v) for v in _impl._coerce_series(low, "low")]
    return [_RegBar(high=h, low=lo) for h, lo in zip(highs, lows)]


def _cv_bars(close, volume) -> list:
    """Build registry bars from just close/volume (OBV). Other fields default to 0.0.

    OBV reads only ``bar.close``/``bar.volume``; this avoids passing ``close`` into
    the unused high/low slots.
    """
    closes = [float(v) for v in _impl._coerce_series(close, "close")]
    vols = [float(v) for v in _impl._coerce_series(volume, "volume")]
    return [_RegBar(close=c, volume=vol) for c, vol in zip(closes, vols)]


def _ohlc_bars_from_history(history) -> list:
    """Build registry bars from a bar/number ``history`` for OHLC indicators.

    Bar-like elements expose ``high``/``low``/``close``/``volume``; a plain
    number is treated as ``high == low == close == volume == value`` (matching
    the previous accessor, which fed the same numeric sequence to every OHLC
    slot).
    """
    out: list = []
    for b in history:
        if isinstance(b, numbers.Real) and not isinstance(b, bool):
            f = float(b)
            out.append(_RegBar(open=f, high=f, low=f, close=f, volume=f))
        else:
            out.append(
                _RegBar(
                    open=float(getattr(b, "open", 0.0)),
                    high=float(b.high),
                    low=float(b.low),
                    close=float(b.close),
                    volume=float(getattr(b, "volume", 0.0)),
                )
            )
    return out


def _scalar(value: Optional[float]) -> float:
    """Map the registry's warm-up ``None`` to the scalar contract's ``0.0``."""
    return 0.0 if value is None else value


def sma(data, period) -> float:
    """Latest Simple Moving Average value. See module contract."""
    return _scalar(IndicatorRegistry().sma(_value_bars(data), int(period), source="close"))


def ema(data, period) -> float:
    """Latest Exponential Moving Average value. See module contract."""
    return _scalar(IndicatorRegistry().ema(_value_bars(data), int(period), source="close"))


def rsi(data, period=14) -> float:
    """Latest Relative Strength Index value. See module contract."""
    return _scalar(IndicatorRegistry().rsi(_value_bars(data), int(period), source="close"))


def macd(data, fast=12, slow=26, signal=9) -> tuple[float, float, float]:
    """Latest (MACD line, signal line, histogram) values. See module contract."""
    bars = _value_bars(data)
    reg = IndicatorRegistry()
    f, s, g = int(fast), int(slow), int(signal)
    return (
        _scalar(reg.macd(bars, fast=f, slow=s, signal=g, source="close", select="macd")),
        _scalar(reg.macd(bars, fast=f, slow=s, signal=g, source="close", select="signal")),
        _scalar(reg.macd(bars, fast=f, slow=s, signal=g, source="close", select="histogram")),
    )


def bollinger_bands(data, period=20, num_std=2.0) -> tuple[float, float, float]:
    """Latest (upper, middle, lower) Bollinger Band values. See module contract."""
    bars = _value_bars(data)
    reg = IndicatorRegistry()
    p, n = int(period), float(num_std)
    return (
        _scalar(reg.bollinger_bands(bars, period=p, num_std=n, source="close", select="upper")),
        _scalar(reg.bollinger_bands(bars, period=p, num_std=n, source="close", select="middle")),
        _scalar(reg.bollinger_bands(bars, period=p, num_std=n, source="close", select="lower")),
    )


def atr(high, low, close, period=14) -> float:
    """Latest Average True Range value. See module contract."""
    return _scalar(IndicatorRegistry().atr(_ohlc_bars(high, low, close), period=int(period)))


def adx(high, low, close, period=14) -> float:
    """Latest Average Directional Index value. See module contract."""
    return _scalar(IndicatorRegistry().adx(_ohlc_bars(high, low, close), period=int(period)))


def stochastic(high, low, close, k_period=14, d_period=3) -> tuple[float, float]:
    """Latest (%K, %D) Stochastic Oscillator values. See module contract."""
    bars = _ohlc_bars(high, low, close)
    reg = IndicatorRegistry()
    k, d = int(k_period), int(d_period)
    return (
        _scalar(reg.stochastic(bars, k_period=k, d_period=d, select="k")),
        _scalar(reg.stochastic(bars, k_period=k, d_period=d, select="d")),
    )


def vwap(high, low, close, volume) -> float:
    """Latest cumulative VWAP value. See module contract."""
    return _scalar(IndicatorRegistry().vwap(_ohlc_bars(high, low, close, volume)))


def donchian_channels(high, low, period=20) -> tuple[float, float, float]:
    """Latest (upper, middle, lower) Donchian channel values. See module contract."""
    bars = _hl_bars(high, low)
    reg = IndicatorRegistry()
    p = int(period)
    return (
        _scalar(reg.donchian(bars, period=p, select="upper")),
        _scalar(reg.donchian(bars, period=p, select="middle")),
        _scalar(reg.donchian(bars, period=p, select="lower")),
    )


def keltner_channels(
    high, low, close, period=20, atr_period=10, multiplier=2.0
) -> tuple[float, float, float]:
    """Latest (upper, middle, lower) Keltner channel values. See module contract."""
    bars = _ohlc_bars(high, low, close)
    reg = IndicatorRegistry()
    p, ap, m = int(period), int(atr_period), float(multiplier)
    return (
        _scalar(reg.keltner(bars, period=p, atr_period=ap, multiplier=m, select="upper")),
        _scalar(reg.keltner(bars, period=p, atr_period=ap, multiplier=m, select="middle")),
        _scalar(reg.keltner(bars, period=p, atr_period=ap, multiplier=m, select="lower")),
    )


def obv(close, volume) -> float:
    """Latest On-Balance Volume value. See module contract."""
    return _scalar(IndicatorRegistry().obv(_cv_bars(close, volume)))


def mfi(high, low, close, volume, period=14) -> float:
    """Latest Money Flow Index value. See module contract."""
    return _scalar(
        IndicatorRegistry().mfi(_ohlc_bars(high, low, close, volume), period=int(period))
    )


def roc(data, period=12) -> float:
    """Latest Rate of Change (percent) value. See module contract."""
    return _scalar(IndicatorRegistry().roc(_value_bars(data), int(period), source="close"))


def cci(high, low, close, period=20) -> float:
    """Latest Commodity Channel Index value. See module contract."""
    return _scalar(IndicatorRegistry().cci(_ohlc_bars(high, low, close), period=int(period)))


def williams_r(high, low, close, period=14) -> float:
    """Latest Williams %R value. See module contract."""
    return _scalar(IndicatorRegistry().williams_r(_ohlc_bars(high, low, close), period=int(period)))


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
    {
        "sma",
        "ema",
        "rsi",
        "macd",
        "bollinger",
        "atr",
        "adx",
        "stochastic",
        "vwap",
        "donchian",
        "keltner",
        "obv",
        "mfi",
        "roc",
        "cci",
        "williams_r",
    }
)
_VALID_SOURCES: frozenset[str] = frozenset(
    {"close", "open", "high", "low", "volume", "hl2", "ohlc4"}
)

# Per-indicator param validators, mirroring spec_dsl's _INDICATOR_PARAM_SPECS
# (required ∪ optional; ``source`` is a dedicated argument and excluded). Kept
# as a literal table because the flat sandbox copy cannot import spec_dsl. Used
# both to reject unexpected/typo'd keys and to validate values — so a read that
# the static gate cannot inspect (dynamic param, or ``**kwargs`` unpacking) is
# still rejected at runtime with a contract ``ValueError`` rather than silently
# coercing an out-of-DSL value (e.g. ``period=1.5``/``'20'``) via ``int(...)``.
# NB: must stay in sync with spec_dsl._INDICATOR_PARAM_SPECS.


def _int_in(lo: int, hi: int):
    def check(value) -> None:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"expected an int in [{lo}, {hi}], got {value!r}")
        if not (lo <= value <= hi):
            raise ValueError(f"expected an int in [{lo}, {hi}], got {value}")

    return check


def _float_gt(lo: float):
    def check(value) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"expected a number > {lo}, got {value!r}")
        # Non-finite values (inf/nan) are rejected to match spec_dsl's float
        # validator — they otherwise produce infinite/NaN indicator output.
        if not (math.isfinite(float(value)) and float(value) > lo):
            raise ValueError(f"expected a finite number > {lo}, got {value}")

    return check


def _one_of(*allowed: str):
    options = frozenset(allowed)

    def check(value) -> None:
        if value not in options:
            raise ValueError(f"expected one of {sorted(options)}, got {value!r}")

    return check


_INDICATOR_PARAM_VALIDATORS: dict[str, dict[str, "object"]] = {
    "sma": {"period": _int_in(2, 400)},
    "ema": {"period": _int_in(2, 400)},
    "rsi": {"period": _int_in(2, 200)},
    "macd": {
        "fast": _int_in(2, 200),
        "slow": _int_in(3, 400),
        "signal": _int_in(2, 100),
        "output": _one_of("macd", "signal", "histogram"),
    },
    "bollinger": {
        "period": _int_in(5, 200),
        "num_std": _float_gt(0),
        "band": _one_of("upper", "middle", "lower", "percent_b", "bandwidth"),
    },
    "atr": {"period": _int_in(2, 200)},
    "adx": {"period": _int_in(2, 200)},
    "stochastic": {
        "k_period": _int_in(2, 200),
        "d_period": _int_in(1, 100),
        "output": _one_of("k", "d"),
    },
    "vwap": {},
    "donchian": {
        "period": _int_in(2, 400),
        "band": _one_of("upper", "middle", "lower"),
    },
    "keltner": {
        "period": _int_in(2, 400),
        "atr_period": _int_in(2, 200),
        "multiplier": _float_gt(0),
        "band": _one_of("upper", "middle", "lower"),
    },
    "obv": {},
    "mfi": {"period": _int_in(2, 200)},
    "roc": {"period": _int_in(2, 400)},
    "cci": {"period": _int_in(2, 400)},
    "williams_r": {"period": _int_in(2, 200)},
}


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
    shadow (``_ShadowContext``). Routes through the streaming
    ``IndicatorRegistry`` — the same recurrences ``StreamingHistoryView`` runs
    per bar — so the returned value is byte-identical to the engine's trailing
    value for the same bars (a fresh registry's cold value equals the streamed
    value; see ``tests/test_streaming_indicators.py``).

    Cost note: this is a stateless accessor — it cold-starts a fresh registry
    and projects ``history`` each call, so cost is O(len(history)) per call.
    That matches the prior pandas accessor and is fine for ad-hoc and shadow
    use, but it is NOT the engine's per-bar path: the engine reads indicators
    through :class:`StreamingHistoryView`, which retains the registry and is
    O(window) per bar. A ``StrategyContext`` that wants O(window) per-bar reads
    from ``ctx.indicator`` should retain a registry/view rather than call this
    repeatedly (a possible follow-up, out of scope here).

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
    # Reject unexpected/typo'd keys and validate values up front (independent of
    # warm-up), so a mis-parameterized read raises a contract ValueError rather
    # than silently coercing an out-of-DSL value — this is the only guard for
    # dynamic params the static conformance gate cannot inspect.
    validators = _INDICATOR_PARAM_VALIDATORS[name]
    unexpected = set(params) - set(validators)
    if unexpected:
        raise ValueError(
            f"indicator {name!r} got unexpected param(s) {sorted(unexpected)}; "
            f"allowed: {sorted(validators)}"
        )
    for _key, _value in params.items():
        validators[_key](_value)
    if not history:
        return None

    reg = IndicatorRegistry()

    if name in ("sma", "ema"):
        if "period" not in params:
            raise ValueError(f"indicator {name!r} requires a 'period' param")
        bars = _value_bars(_source_values(history, source))
        method = reg.sma if name == "sma" else reg.ema
        return method(bars, period=int(params["period"]), source="close")

    if name == "rsi":
        bars = _value_bars(_source_values(history, source))
        return reg.rsi(bars, period=int(params.get("period", 14)), source="close")

    if name == "macd":
        bars = _value_bars(_source_values(history, source))
        # Selector value already validated against the allowed set above.
        return reg.macd(
            bars,
            fast=int(params.get("fast", 12)),
            slow=int(params.get("slow", 26)),
            signal=int(params.get("signal", 9)),
            source="close",
            select=str(params.get("output", "macd")),
        )

    if name == "bollinger":
        bars = _value_bars(_source_values(history, source))
        return reg.bollinger_bands(
            bars,
            period=int(params.get("period", 20)),
            num_std=float(params.get("num_std", 2.0)),
            source="close",
            select=str(params.get("band", "middle")),
        )

    if name == "roc":
        # ``_source_values`` already projects the requested ``source`` into a flat
        # series, and ``_value_bars`` lands it in each bar's ``close`` slot — so the
        # registry must read ``source="close"`` here to consume exactly that
        # projected series. This mirrors the sma/ema/rsi/macd/bollinger branches
        # above; the bars carry only a ``close`` field, not the original OHLC.
        bars = _value_bars(_source_values(history, source))
        return reg.roc(bars, period=int(params.get("period", 12)), source="close")

    # OHLC-sourced indicators read their fields directly and forbid a `source`
    # override (mirrors spec_dsl's allow_source=False for these names). Reject a
    # non-default source rather than silently computing a different indicator
    # than the caller requested — otherwise check #1 would credit the read by
    # name and the strategy would run mis-sourced instead of being refined.
    if source != "close":
        raise ValueError(f"indicator {name!r} does not accept a 'source' override")
    ohlc = _ohlc_bars_from_history(history)
    if name == "atr":
        return reg.atr(ohlc, period=int(params.get("period", 14)))
    if name == "adx":
        return reg.adx(ohlc, period=int(params.get("period", 14)))
    if name == "stochastic":
        return reg.stochastic(
            ohlc,
            k_period=int(params.get("k_period", 14)),
            d_period=int(params.get("d_period", 3)),
            select=str(params.get("output", "k")),
        )
    if name == "donchian":
        return reg.donchian(
            ohlc,
            period=int(params.get("period", 20)),
            select=str(params.get("band", "middle")),
        )
    if name == "keltner":
        return reg.keltner(
            ohlc,
            period=int(params.get("period", 20)),
            atr_period=int(params.get("atr_period", 10)),
            multiplier=float(params.get("multiplier", 2.0)),
            select=str(params.get("band", "middle")),
        )
    if name == "obv":
        return reg.obv(ohlc)
    if name == "mfi":
        return reg.mfi(ohlc, period=int(params.get("period", 14)))
    if name == "cci":
        return reg.cci(ohlc, period=int(params.get("period", 20)))
    if name == "williams_r":
        return reg.williams_r(ohlc, period=int(params.get("period", 14)))

    # name == "vwap" (only remaining valid name)
    return reg.vwap(ohlc)
