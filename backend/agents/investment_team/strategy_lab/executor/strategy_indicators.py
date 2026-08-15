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

import contextvars
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
    from ..indicators.streaming import IndicatorRegistry, _safe_getattr, resolve_indicator
except ImportError:  # flat sandbox layout
    from _streaming_indicators import (  # type: ignore[no-redef]
        IndicatorRegistry,
        _safe_getattr,
        resolve_indicator,
    )

# ``new_registry`` builds an ``IndicatorRegistry`` wired to the active batch
# indicator cache when one is bound (see ``batch_cache_context``). In the flat
# sandbox subprocess ``batch_cache_context`` is not copied in — and a separate
# process could not share the parent's in-memory cache anyway — so fall back to
# a plain constructor there, leaving ``_shared_registry`` behaving exactly as
# before in the sandbox.
try:  # in-package use
    from ..batch_cache_context import new_registry
except ImportError:  # pragma: no cover - flat sandbox layout has no batch cache

    def new_registry():  # type: ignore[no-redef]
        return IndicatorRegistry()


# Ambient "which execution is dispatching strategy code right now" signal for
# the 16 wrapper functions below (and any indicator_value call with no
# explicit `registries`). StrategyContext/_ShadowContext each bracket their
# calls into strategy code with `_active_registries.set(self._indicator_
# registries)` / `.reset(token)` (see streaming_harness.py's _HARNESS_SCRIPT
# and predicate_conformance.py's _check_fixture), so a standalone wrapper
# called from inside on_start/on_bar/on_fill/on_end resolves to that
# execution's own dict — the same one ctx.indicator() already uses — instead
# of a dict shared with whatever other execution last happened to run on this
# thread. Unlike a plain thread-local, a contextvar Token records exactly
# what to restore on exit, so nested/alternating set-call-reset regions never
# leak into each other regardless of how the call stack interleaves.
#
# A wrapper call made with neither an explicit `registries` argument nor
# `_active_registries` set — i.e. outside any dispatch bracket entirely, such
# as a direct script/test call with no owning StrategyContext/_ShadowContext
# at all — gets a fresh, uncached IndicatorRegistry every time (see
# _shared_registry below) rather than falling back to a third, module-level
# cache. An earlier version of this module used a thread-local dict for that
# case, reasoning it was safe because such a caller has no context to
# interleave with — true for interleaving, but not for two *sequential*
# ad-hoc calls: `bollinger_bands(history_a)` followed by
# `bollinger_bands(history_b)` for the same symbol with overlapping
# timestamps could still reuse history_a's cached deque and return a value
# blended from both histories, silently, with no context object involved at
# all. Since every real caller now has a registries source — StrategyContext/
# _ShadowContext always bracket dispatch — there is no remaining case where
# that third cache would be both safe and beneficial, so it was removed
# rather than patched further.
# Set to a dict for the duration of a bracketed call; None outside one.
_active_registries: "contextvars.ContextVar[Optional[dict]]" = contextvars.ContextVar(
    "_active_registries", default=None
)


def _trailing_element(reference):
    """Return ``reference``'s trailing element, or ``None`` if unavailable.

    Preconditions: none — ``reference`` may be any shape ``_coerce_series``
    accepts, ``None``, or a generator/iterator.
    Postconditions: never raises and never consumes an iterator. Returns the
    last element for a non-empty pandas ``Series`` (via ``.iloc``, always
    positional) or a non-empty list/tuple/deque (via ``[-1]``); returns
    ``None`` for ``None``, an empty sequence, or anything without stable
    positional access (e.g. a generator) — that source is left untouched for
    the caller's own ``_coerce_series``/materialisation to consume exactly
    once.
    """
    if reference is None:
        return None
    if hasattr(reference, "iloc"):  # pandas Series: .iloc is always positional
        return reference.iloc[-1] if len(reference) else None
    if hasattr(reference, "__len__") and hasattr(reference, "__getitem__"):
        return reference[-1] if len(reference) else None
    return None


def _shared_registry(
    reference, *, source: str = "close", registries: Optional[dict] = None
) -> IndicatorRegistry:
    """Return a cached IndicatorRegistry for ``reference``'s ``(symbol, source)``.

    Symbol/timestamp inference is reliable for ``indicator_value`` (its
    ``history`` argument is always a real ``Bar``-like sequence keyed by
    symbol on both production paths) but only best-effort for the 16 wrapper
    functions below: a caller that pre-slices bars into separate plain-number
    arrays (e.g. ``highs = [b.high for b in bars]``) before calling loses
    both before they ever reach this helper — those calls fall through to a
    fresh, uncached instance every time (see below), identical to today's
    per-call behavior for that call shape.

    Sharing is gated on the trailing element carrying *both* a ``timestamp``
    and a ``symbol`` — not one or the other. IndicatorRegistry's own
    bar-fingerprint keys off ``(id(last_bar), len(bars), timestamp, close)``,
    with ``close`` a *conditional fallback that only fires when timestamp is
    absent on both sides* (see ``streaming.py``'s ``_advance_kind``
    docstring). The ``_RegBar`` objects these helpers build are fresh,
    ephemeral, and discarded every call — CPython commonly reuses a
    just-freed object's ``id()`` for the next same-sized allocation — so
    without a real timestamp, two calls for genuinely different data that
    happen to share a length and a trailing close value could be misread as
    the same bar (or the same stream advancing by one), returning a stale
    cached value instead of recomputing. A timestamp alone isn't enough
    either: a bar-like object can carry ``timestamp`` without ``symbol`` (the
    16 wrappers only require whichever field ``_coerce_series`` needs), and
    two unrelated symbol-less-but-timestamped streams sharing one bucket has
    the same collision risk. When either is missing, this returns a fresh,
    uncached ``IndicatorRegistry`` instead — no caching benefit for that call
    shape, but never a wrong value.

    ``source`` distinguishes registry entries for the *same* history read
    with different projections. ``indicator_value`` always dispatches to the
    registry with the literal ``source="close"`` (the caller's requested
    source is pre-projected onto ``_RegBar.close`` before the registry ever
    sees it — see ``_source_values``), so the registry's own cache key can't
    tell "sma of high" apart from "sma of close" for the same bars. If a
    bar's high happens to equal its close, the two projections' fingerprints
    can coincide entirely. Bucketing by ``(symbol, source)`` — one level
    above the registry — keeps those reads in separate registries. The 16
    wrapper functions have no ``source`` concept (they always read whatever
    field the caller passed as "the" series) and all share the default.

    ``len(reference)`` is *also* part of the bucket key, but **only** on the
    ``_active_registries`` fallback path (``registries is None`` on entry) —
    not when a caller passes ``registries`` explicitly. The two production
    callers that pass ``registries`` explicitly (``StrategyContext.indicator()``
    and ``_ShadowContext.indicator()``, see ``contract.py``/
    ``predicate_conformance.py``) always hand this function their own
    ``history`` — the *full*, internally-managed retained history for that
    symbol, never a caller-chosen slice. ``StrategyContext._ingest_bar``
    grows it by exactly one bar per ingested bar and then holds it fixed at
    ``STREAMING_WINDOW_BARS`` (``runtime_window.py``) once that cap is
    reached, so its length is a deterministic function of how many bars have
    been ingested so far, not something two different call sites can pick
    independently for the same symbol — the "same symbol, two different
    window depths" collision the length component exists to prevent cannot
    occur here, and length-bucketing this path anyway would only cost an
    extra registry construction per bar during warm-up for zero safety
    benefit, permanently defeating the incremental-caching win on
    ``ctx.indicator()``, the primary prescribed hot-loop path this change
    targets.

    The wrapper functions below (and any direct ``indicator_value`` call
    with no context in scope) are exactly the opposite: they take no
    ``registries`` parameter of their own, so they always resolve through
    ``_active_registries``, and a caller *can* legitimately request two
    different window depths for the same symbol in the same execution — e.g.
    ``bollinger_bands(ctx.history(sym, 20))`` then
    ``bollinger_bands(ctx.history(sym, 50))`` for the same bar — passing
    genuinely different-length bar sequences that would otherwise share one
    registry under plain ``(symbol, source)``. ``IndicatorRegistry``'s own
    ``_advance_kind`` checks ``id(bars[-2])`` against its cached fingerprint
    *before* checking timestamps (an ``elif``, not a joint condition) and
    treats an ``id()`` match alone as sufficient proof of "same bar" even
    when the timestamps, had they been checked, would have correctly
    disagreed — and empirically, for two same-shaped ``_RegBar`` lists built
    back-to-back with no other same-size allocation between them, CPython's
    allocator reuses that address far more often than not (reproduced 100%
    across 2000 trials for a `bollinger_bands` depth-20-then-21 pair, not a
    rare coincidence). Splitting this path's bucket by length sidesteps that
    ``IndicatorRegistry``-internal behavior entirely — out of scope to
    change directly — rather than trying to out-guess it. The cost is
    bounded and one-time: during warm-up, a caller whose depth grows every
    bar (fewer than ``n`` bars retained yet) gets one extra registry per
    distinct length seen, same as any other cold-start; once the depth
    stabilizes, every call hits the same key and the full caching benefit
    applies exactly as before.

    ``registries`` is the dict this call reads/writes into: pass the owning
    execution context's own dict (``StrategyContext``/``_ShadowContext`` each
    hold one, see ``contract.py``/``predicate_conformance.py``) so that
    instance's cache is never visible to any other context — including two
    contexts for the *same* symbol constructed on the same thread whose bar
    ingestion happens to interleave rather than one running to completion
    before the next starts.

    Callers that can't pass ``registries`` directly — the 16 wrapper
    functions below, and any direct ``indicator_value`` call with no context
    in scope — fall back to ``_active_registries`` (a
    ``contextvars.ContextVar``): set by ``StrategyContext``/``_ShadowContext``
    for the duration of a call into strategy code (``on_start``/``on_bar``/
    ``on_fill``/``on_end`` — see ``streaming_harness.py``'s
    ``_HARNESS_SCRIPT`` and ``predicate_conformance.py``'s
    ``_check_fixture``), so a standalone wrapper called *from inside* that
    dispatch resolves to the same dict ``ctx.indicator()`` already uses. This
    is what makes the wrapper functions execution-safe despite taking no
    ``registries`` parameter of their own: a plain thread-local dict can't
    distinguish two different execution contexts that happen to run on the
    same thread (nothing about "same thread" implies "same execution" once
    more than one context can be live at a time — and, less obviously, not
    even "one call, then a later, unrelated call" is safe without something
    marking the boundary between them), but a contextvar ``Token`` records
    exactly what to restore when a bracketed call exits, so alternating
    dispatch between two contexts — even genuinely interleaved — never leaks
    one's cache into the other's.

    A call with *neither* ``registries`` nor ``_active_registries`` set —
    entirely outside any dispatch bracket, e.g. a direct script or test call
    with no owning ``StrategyContext``/``_ShadowContext`` at all — gets a
    fresh, uncached ``IndicatorRegistry`` (see below), never a shared one.
    There is deliberately no further fallback cache for this case: two
    *sequential* ad-hoc calls sharing a symbol (not just interleaved ones)
    can have genuinely different histories with an overlapping timestamp —
    e.g. ``bollinger_bands(history_a)`` then ``bollinger_bands(history_b)``
    for the same symbol where ``history_b``'s second-to-last bar happens to
    carry the same timestamp as ``history_a``'s last bar — which
    ``streaming.py``'s ``_advance_kind`` reads as "this stream just advanced
    by one bar," blending both histories into a wrong value. That failure
    mode has no context object involved at any point, so no amount of
    resetting-at-construction can catch it; only never caching this call
    shape at all does.

    Preconditions:
        ``reference`` is whatever pre-projection argument the caller already
        has in scope (``data``/``high``/``low``/``close``/``history``) — any
        shape ``_coerce_series`` accepts, or ``None``. ``source`` is the
        caller's requested source string (or the default ``"close"`` for
        callers with no source concept). ``registries``, when given, is a
        dict this call may read and write (typically an execution context's
        own, initially-empty dict). Never raises.
    Postconditions:
        Returns an ``IndicatorRegistry``. When the trailing element exposes
        both a non-``None`` ``timestamp`` and a non-``None`` ``symbol``: if
        ``registries`` was passed explicitly (not ``None``), constructs and
        caches one per ``(symbol, source)`` in that dict; otherwise, when the
        current ``_active_registries`` resolves to a dict, constructs and
        caches one per ``(symbol, source, len(reference))`` in that dict —
        either way, the first time the key is seen, and returns that same
        instance on every subsequent call for that key against that same
        dict. Otherwise (missing timestamp/symbol, or no dict available via
        either path) returns a fresh, never-cached instance. Never mutates
        ``reference``.
    """
    last = _trailing_element(reference)
    if last is None:
        return new_registry()
    # _safe_getattr (not plain getattr) because this metadata was never read
    # at all before sharing existed — a lazily-loaded timestamp/symbol
    # descriptor that raises on access must degrade to "unavailable", the
    # same as IndicatorRegistry's own bar reads, not crash a call that
    # worked fine when every call got a disposable, cold registry.
    timestamp = (
        last.get("timestamp") if isinstance(last, dict) else _safe_getattr(last, "timestamp")
    )
    if timestamp is None:
        return new_registry()
    symbol = last.get("symbol") if isinstance(last, dict) else _safe_getattr(last, "symbol")
    if symbol is None:
        return new_registry()
    if registries is not None:
        key = (symbol, source)
    else:
        registries = _active_registries.get()
        if registries is None:
            return new_registry()
        key = (symbol, source, len(reference))
    reg = registries.get(key)
    if reg is None:
        reg = new_registry()
        registries[key] = reg
    return reg


def _extract_timestamps(source) -> list:
    """Best-effort per-element ``timestamp``, aligned with ``source``.

    Mirrors the shapes ``_coerce_series`` accepts, reading each element's
    ``timestamp`` instead of a numeric field, so the ``_RegBar`` objects built
    from ``source`` carry the same stable per-bar signal :func:`_shared_registry`
    keys sharing on — without it, IndicatorRegistry's fingerprint has nothing
    but a coincidental close-value (and possibly-reused ``id()``) to tell two
    genuinely different bar windows apart (see :func:`_shared_registry`).

    Preconditions:
        ``source`` is any shape ``_coerce_series`` accepts, or ``None``.
    Postconditions:
        Returns a list the same length as ``source`` when it is a
        list/tuple/deque/``pd.Series``, each entry the element's
        ``timestamp`` (attribute or dict key) or ``None`` when absent or
        when reading it raises (via :func:`_safe_getattr` — a lazily-loaded
        descriptor that misbehaves degrades to "no timestamp" here, not a
        crash). Never raises and never consumes a generator/iterator —
        returns ``[]`` for ``None`` or anything without stable positional
        access, leaving it untouched for the caller's own ``_coerce_series``
        to consume exactly once.
    """
    if source is None:
        return []
    if not (
        hasattr(source, "iloc") or (hasattr(source, "__len__") and hasattr(source, "__getitem__"))
    ):
        return []
    return [
        (elem.get("timestamp") if isinstance(elem, dict) else _safe_getattr(elem, "timestamp"))
        for elem in source
    ]


class _RegBar:
    """Minimal bar exposing the OHLCV attributes the registry reads.

    The registry computes against bar objects (``bar.close``/``bar.high``/…); we
    project the caller's price sequence(s) onto these so the scalar API and the
    accessor reuse the exact recurrence the engine view uses. ``symbol`` is not
    tracked here — :func:`_shared_registry` buckets by symbol at the registry
    level instead, so the registry never needs to see it on the bar. But
    ``timestamp`` matters: these ``_RegBar`` objects are freshly built and
    discarded every call, so once a registry is shared across calls (see
    :func:`_shared_registry`), a real per-bar timestamp is what lets the
    registry's own fingerprinting (``_safe_getattr``-based) tell a genuinely
    new bar window apart from a coincidentally-similar one, rather than
    relying on a fresh call never having a prior fingerprint to collide with.
    """

    __slots__ = ("open", "high", "low", "close", "volume", "timestamp")

    def __init__(
        self,
        *,
        open: float = 0.0,
        high: float = 0.0,
        low: float = 0.0,
        close: float = 0.0,
        volume: float = 0.0,
        timestamp: Optional[str] = None,
    ) -> None:
        self.open = open
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume
        self.timestamp = timestamp


def _project_bars(_timestamps=None, **fields) -> list:
    """Build registry bars from named OHLCV field sequences; omitted fields default to 0.0.

    Single source of truth for the call shapes the scalar wrappers need: an indicator
    that reads only a subset (Donchian: high/low; OBV: close/volume) passes just those
    fields and gets no placeholder in the slots it never reads.

    Preconditions: each keyword in ``fields`` is a ``_RegBar`` field name (``open``/
    ``high``/``low``/``close``/``volume``) mapped to a sequence ``_coerce_series``
    accepts; a non-field keyword raises ``TypeError`` from the ``_RegBar(**…)``
    construction. ``_timestamps``, when given, is any sequence (typically from
    :func:`_extract_timestamps`).
    Postconditions: returns one ``_RegBar`` per position, with each provided series
    coerced to floats and zipped positionally (stopping at the shortest, matching the
    prior per-shape builders — ``_timestamps`` included in that alignment when given);
    an omitted field keeps ``_RegBar``'s ``0.0`` default on every bar, and a missing/
    exhausted ``_timestamps`` entry keeps its ``None`` default. Empty ``fields`` yields
    ``[]``.
    """
    names = list(fields)
    columns = [[float(v) for v in _impl._coerce_series(fields[name], name)] for name in names]
    if not _timestamps:
        return [_RegBar(**dict(zip(names, row))) for row in zip(*columns)]
    return [
        _RegBar(timestamp=ts, **dict(zip(names, row)))
        for row, ts in zip(zip(*columns), _timestamps)
    ]


def _value_bars(values, timestamps=None) -> list:
    """Project a single source series onto close-only registry bars.

    ``values`` is any shape ``_coerce_series`` accepts; the projected scalar lands in
    ``close`` so a registry call with ``source="close"`` reads exactly that series.
    ``timestamps``, when given, overrides the timestamps extracted from ``values``
    itself — needed by callers (``indicator_value``) whose ``values`` argument has
    already been stripped down to plain numbers by an intermediate projection step,
    so the original bars' timestamps must be threaded through explicitly instead.
    """
    ts = timestamps if timestamps is not None else _extract_timestamps(values)
    return _project_bars(close=values, _timestamps=ts)


def _ohlc_bars(high, low, close, volume=None) -> list:
    """Zip separate OHLC(V) sequences into registry bars (atr/adx/stochastic/vwap/…)."""
    ts = _extract_timestamps(close)
    if volume is None:
        return _project_bars(high=high, low=low, close=close, _timestamps=ts)
    return _project_bars(high=high, low=low, close=close, volume=volume, _timestamps=ts)


def _ohlc_bars_from_history(history) -> list:
    """Build registry bars from a bar/number ``history`` for OHLC indicators.

    Bar-like elements expose ``high``/``low``/``close``/``volume``; a plain
    number is treated as ``high == low == close == volume == value`` (matching
    the previous accessor, which fed the same numeric sequence to every OHLC
    slot). Bar-like elements' ``timestamp`` is propagated onto each ``_RegBar``
    (see :func:`_shared_registry`); a plain number has none to propagate.
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
                    timestamp=_safe_getattr(b, "timestamp"),
                )
            )
    return out


def _scalar(value: Optional[float]) -> float:
    """Map the registry's warm-up ``None`` to the scalar contract's ``0.0``."""
    return 0.0 if value is None else value


def sma(data, period) -> float:
    """Latest Simple Moving Average value. See module contract."""
    return _scalar(_shared_registry(data).sma(_value_bars(data), int(period), source="close"))


def ema(data, period) -> float:
    """Latest Exponential Moving Average value. See module contract."""
    return _scalar(_shared_registry(data).ema(_value_bars(data), int(period), source="close"))


def rsi(data, period=14) -> float:
    """Latest Relative Strength Index value. See module contract."""
    return _scalar(_shared_registry(data).rsi(_value_bars(data), int(period), source="close"))


def macd(data, fast=12, slow=26, signal=9) -> tuple[float, float, float]:
    """Latest (MACD line, signal line, histogram) values. See module contract."""
    bars = _value_bars(data)
    reg = _shared_registry(data)
    f, s, g = int(fast), int(slow), int(signal)
    return (
        _scalar(reg.macd(bars, fast=f, slow=s, signal=g, source="close", select="macd")),
        _scalar(reg.macd(bars, fast=f, slow=s, signal=g, source="close", select="signal")),
        _scalar(reg.macd(bars, fast=f, slow=s, signal=g, source="close", select="histogram")),
    )


def bollinger_bands(data, period=20, num_std=2.0) -> tuple[float, float, float]:
    """Latest (upper, middle, lower) Bollinger Band values. See module contract."""
    bars = _value_bars(data)
    reg = _shared_registry(data)
    p, n = int(period), float(num_std)
    return (
        _scalar(reg.bollinger_bands(bars, period=p, num_std=n, source="close", select="upper")),
        _scalar(reg.bollinger_bands(bars, period=p, num_std=n, source="close", select="middle")),
        _scalar(reg.bollinger_bands(bars, period=p, num_std=n, source="close", select="lower")),
    )


def atr(high, low, close, period=14) -> float:
    """Latest Average True Range value. See module contract."""
    return _scalar(_shared_registry(close).atr(_ohlc_bars(high, low, close), period=int(period)))


def adx(high, low, close, period=14) -> float:
    """Latest Average Directional Index value. See module contract."""
    return _scalar(_shared_registry(close).adx(_ohlc_bars(high, low, close), period=int(period)))


def stochastic(high, low, close, k_period=14, d_period=3) -> tuple[float, float]:
    """Latest (%K, %D) Stochastic Oscillator values. See module contract."""
    bars = _ohlc_bars(high, low, close)
    reg = _shared_registry(close)
    k, d = int(k_period), int(d_period)
    return (
        _scalar(reg.stochastic(bars, k_period=k, d_period=d, select="k")),
        _scalar(reg.stochastic(bars, k_period=k, d_period=d, select="d")),
    )


def vwap(high, low, close, volume, period=20) -> float:
    """Latest rolling-window VWAP value (default 20-bar window). See module contract.

    Uses the same rolling ``period`` semantics as the DSL / ``ctx.indicator``
    / engine paths (``reg.vwap(bars, period=...)``), so a value read here is
    byte-identical to the engine's trailing VWAP for the same bars — the
    module's cross-path invariant. (The scalar was cumulative before VWAP's
    rolling-window unification; it now matches every other VWAP surface.)
    """
    return _scalar(
        _shared_registry(close).vwap(_ohlc_bars(high, low, close, volume), period=int(period))
    )


def donchian_channels(high, low, period=20) -> tuple[float, float, float]:
    """Latest (upper, middle, lower) Donchian channel values. See module contract."""
    bars = _project_bars(high=high, low=low, _timestamps=_extract_timestamps(high))
    reg = _shared_registry(high)  # no close/volume arg here to key off instead
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
    reg = _shared_registry(close)
    p, ap, m = int(period), int(atr_period), float(multiplier)
    return (
        _scalar(reg.keltner(bars, period=p, atr_period=ap, multiplier=m, select="upper")),
        _scalar(reg.keltner(bars, period=p, atr_period=ap, multiplier=m, select="middle")),
        _scalar(reg.keltner(bars, period=p, atr_period=ap, multiplier=m, select="lower")),
    )


def obv(close, volume) -> float:
    """Latest On-Balance Volume value. See module contract."""
    bars = _project_bars(close=close, volume=volume, _timestamps=_extract_timestamps(close))
    return _scalar(_shared_registry(close).obv(bars))


def mfi(high, low, close, volume, period=14) -> float:
    """Latest Money Flow Index value. See module contract."""
    return _scalar(
        _shared_registry(close).mfi(_ohlc_bars(high, low, close, volume), period=int(period))
    )


def roc(data, period=12) -> float:
    """Latest Rate of Change (percent) value. See module contract.

    ``roc`` is source-aware, but — like the other source-aware scalar wrappers
    (:func:`sma`, :func:`ema`, :func:`rsi`) — this helper takes ``data`` already
    projected onto a single series and reads it via the registry's ``close``
    slot. Source selection is the accessor's job: use
    ``indicator_value("roc", history, source=...)`` / ``ctx.indicator(...)`` to
    compute ROC over a non-close source.
    """
    return _scalar(_shared_registry(data).roc(_value_bars(data), int(period), source="close"))


def cci(high, low, close, period=20) -> float:
    """Latest Commodity Channel Index value. See module contract."""
    return _scalar(_shared_registry(close).cci(_ohlc_bars(high, low, close), period=int(period)))


def williams_r(high, low, close, period=14) -> float:
    """Latest Williams %R value. See module contract."""
    return _scalar(
        _shared_registry(close).williams_r(_ohlc_bars(high, low, close), period=int(period))
    )


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
    # ``ctx.indicator("vwap", ...)`` accepts a rolling ``period`` (unified with
    # the factors DSL and synthesis's compiled ``vwap`` helper); the standalone
    # ``vwap()`` scalar function below is unaffected and stays cumulative.
    "vwap": {"period": _int_in(2, 400)},
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
    registries: Optional[dict] = None,
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

    Cost note: ``history`` is still projected fresh each call (O(len(history)),
    bounded by the caller's retention window — see
    ``StrategyContext._ingest_bar``), but the ``IndicatorRegistry`` itself is
    no longer rebuilt per call: :func:`_shared_registry` returns one instance
    reused across calls for the same ``(symbol, source)``, so its bar-
    fingerprint memoization actually takes effect. Repeated reads for the same
    symbol get the same incremental recurrences :class:`StreamingHistoryView`
    runs per bar, instead of a cold recompute every time.

    Preconditions:
        ``name`` is a known DSL indicator (:data:`_VALID_INDICATORS`);
        ``sma``/``ema`` require a ``period`` param; selector params
        (``macd.output``, ``bollinger.band``, ``stochastic.output``) and
        ``source`` are valid for the indicator. Contract violations raise
        ``ValueError`` (a caller bug) — they are never silently coerced.
        ``registries``, when given, should be the caller's own dict (see
        :func:`_shared_registry`) — ``StrategyContext``/``_ShadowContext``
        each pass their own instance attribute so their indicator state can
        never be visible to any other context, however its calls interleave
        with another context's on the same thread.
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

    reg = _shared_registry(history, source=source, registries=registries)

    # Every branch below only extracts/defaults this call's params and picks
    # the right bars projection — the actual name -> IndicatorRegistry method
    # dispatch lives in exactly one place, ``resolve_indicator`` (shared with
    # ``predicate_evaluator._registry_indicator``, which previously carried a
    # second, structurally-parallel 16-way if/elif reaching the same methods).

    # ``_source_values`` strips ``history``'s bar objects down to a flat list
    # of floats, so ``_value_bars`` can no longer read their ``timestamp`` off
    # the (now-numeric) values — extract it from the original ``history``
    # here and thread it through explicitly (see ``_shared_registry``).
    history_timestamps = _extract_timestamps(history)

    if name in ("sma", "ema"):
        if "period" not in params:
            raise ValueError(f"indicator {name!r} requires a 'period' param")
        bars = _value_bars(_source_values(history, source), timestamps=history_timestamps)
        return resolve_indicator(reg, name, bars, source="close", period=int(params["period"]))

    if name == "rsi":
        bars = _value_bars(_source_values(history, source), timestamps=history_timestamps)
        return resolve_indicator(
            reg, name, bars, source="close", period=int(params.get("period", 14))
        )

    if name == "macd":
        bars = _value_bars(_source_values(history, source), timestamps=history_timestamps)
        # Selector value already validated against the allowed set above.
        return resolve_indicator(
            reg,
            name,
            bars,
            source="close",
            fast=int(params.get("fast", 12)),
            slow=int(params.get("slow", 26)),
            signal=int(params.get("signal", 9)),
            output=str(params.get("output", "macd")),
        )

    if name == "bollinger":
        bars = _value_bars(_source_values(history, source), timestamps=history_timestamps)
        return resolve_indicator(
            reg,
            name,
            bars,
            source="close",
            period=int(params.get("period", 20)),
            num_std=float(params.get("num_std", 2.0)),
            band=str(params.get("band", "middle")),
        )

    if name == "roc":
        # ``_source_values`` already projects the requested ``source`` into a flat
        # series, and ``_value_bars`` lands it in each bar's ``close`` slot — so the
        # registry must read ``source="close"`` here to consume exactly that
        # projected series. This mirrors the sma/ema/rsi/macd/bollinger branches
        # above; the bars carry only a ``close`` field, not the original OHLC.
        bars = _value_bars(_source_values(history, source), timestamps=history_timestamps)
        return resolve_indicator(
            reg, name, bars, source="close", period=int(params.get("period", 12))
        )

    # OHLC-sourced indicators read their fields directly and forbid a `source`
    # override (mirrors spec_dsl's allow_source=False for these names). Reject a
    # non-default source rather than silently computing a different indicator
    # than the caller requested — otherwise check #1 would credit the read by
    # name and the strategy would run mis-sourced instead of being refined.
    if source != "close":
        raise ValueError(f"indicator {name!r} does not accept a 'source' override")
    ohlc = _ohlc_bars_from_history(history)
    if name == "atr":
        return resolve_indicator(reg, name, ohlc, period=int(params.get("period", 14)))
    if name == "adx":
        return resolve_indicator(reg, name, ohlc, period=int(params.get("period", 14)))
    if name == "stochastic":
        return resolve_indicator(
            reg,
            name,
            ohlc,
            k_period=int(params.get("k_period", 14)),
            d_period=int(params.get("d_period", 3)),
            output=str(params.get("output", "k")),
        )
    if name == "donchian":
        return resolve_indicator(
            reg,
            name,
            ohlc,
            period=int(params.get("period", 20)),
            band=str(params.get("band", "middle")),
        )
    if name == "keltner":
        return resolve_indicator(
            reg,
            name,
            ohlc,
            period=int(params.get("period", 20)),
            atr_period=int(params.get("atr_period", 10)),
            multiplier=float(params.get("multiplier", 2.0)),
            band=str(params.get("band", "middle")),
        )
    if name == "obv":
        return resolve_indicator(reg, name, ohlc)
    if name == "mfi":
        return resolve_indicator(reg, name, ohlc, period=int(params.get("period", 14)))
    if name == "cci":
        return resolve_indicator(reg, name, ohlc, period=int(params.get("period", 20)))
    if name == "williams_r":
        return resolve_indicator(reg, name, ohlc, period=int(params.get("period", 14)))
    if name == "vwap":
        return resolve_indicator(reg, name, ohlc, period=int(params.get("period", 20)))

    # ``name`` passed the ``_VALID_INDICATORS`` precondition above, so reaching
    # here means a name was added to that table without a dispatch branch. Fail
    # loudly rather than silently returning VWAP's value (a consistent-but-wrong
    # result the conformance shadow and live sandbox would both accept).
    raise ValueError(
        f"indicator_value: no dispatch branch for {name!r} (in _VALID_INDICATORS "
        "but unhandled — add a branch above)."
    )
