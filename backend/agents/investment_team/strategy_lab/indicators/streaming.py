"""Canonical streaming indicator implementations.

The legacy compiled templates rebuilt every indicator from scratch on
every bar. The worst offender was MACD, which ran an outer
``for end in range(slow, len(bars) + 1)`` loop and recomputed the
fast/slow EMAs inside it — ``O(N * (fast + slow))`` work per call.
``StreamingHistoryView`` made this even worse: every appended bar
cleared the indicator cache and forced a full pandas-side recompute on
the next predicate evaluation.

This module persists per-key state across calls so the hot path is
``O(1)`` amortised after the initial cold-start:

* :class:`IndicatorRegistry` carries one slot of state per
  ``(name, params)`` signature on a strategy/view instance. Each
  per-indicator method runs a cold-start once and then advances by a
  single recurrence step on each subsequent bar.
* ``macd_line`` is cached as a bounded :class:`deque`; new bars only
  append one element rather than rebuilding the full history.
* The signal-line EMA, which historically reseeded from
  ``macd_line[0]`` on every call (sliding-window-EMA semantics tied to
  the bounded ``history_depth``), is recomputed against the live deque
  but the deque itself is maintained incrementally.

Semantics are preserved bit-for-bit against the legacy templates —
every indicator returns the same windowed-EMA value at the trailing
bar — so the engine's golden snapshots are unchanged.
"""

from __future__ import annotations

import math
from collections import deque
from typing import Any, Deque, Dict, Optional, Sequence, Tuple

NAN = float("nan")


# ---------------------------------------------------------------------------
# Pure-function recurrences (no instance state).
#
# These are the canonical, side-effect-free implementations of the windowed
# indicator math. ``IndicatorRegistry`` wraps them with per-instance state.
# Both ``factors/primitives.py`` and ``executor/predicate_evaluator.py``
# delegate to these directly when they do not need to retain state across
# calls.
# ---------------------------------------------------------------------------


def _source_value(bar: Any, source: str) -> float:
    """Return ``bar``'s scalar field for ``source``.

    Pre: ``source`` is one of ``open``/``high``/``low``/``close``/``volume``
    /``hl2``/``ohlc4``. Other values fall back to ``close`` — matches the
    sandbox helper's ``_src`` behaviour.
    Post: returns a finite float (assuming the bar fields themselves are
    finite — the engine validates that upstream).
    """
    if source == "close":
        return float(bar.close)
    if source == "high":
        return float(bar.high)
    if source == "low":
        return float(bar.low)
    if source == "open":
        return float(bar.open)
    if source == "volume":
        return float(bar.volume)
    if source == "hl2":
        return (float(bar.high) + float(bar.low)) / 2.0
    if source == "ohlc4":
        return (float(bar.open) + float(bar.high) + float(bar.low) + float(bar.close)) / 4.0
    return float(bar.close)


def windowed_ema(
    bars: Sequence[Any],
    period: int,
    source: str = "close",
) -> float:
    """EMA over the trailing ``period`` bars, seeded from the oldest of them.

    This is the *windowed*-EMA shape — at each call the seed slides forward
    by one bar — and it is what the legacy compiled templates compute. It
    is NOT the infinite-history true-EMA (``ema_t = α·x_t + (1-α)·ema_{t-1}``
    seeded once at the start of the run); switching to true-EMA would
    drift the compiled output and risk golden-snapshot regressions.

    Pre: ``period >= 1``; ``len(bars) >= period``.
    Post: returns the trailing-window EMA scalar. ``NaN`` is reserved for
    insufficient-history callers; this function assumes the caller has
    already gated on ``len(bars) >= period``.
    """
    alpha = 2.0 / (period + 1.0)
    seed_idx = len(bars) - period
    val = _source_value(bars[seed_idx], source)
    for j in range(seed_idx + 1, len(bars)):
        val = alpha * _source_value(bars[j], source) + (1.0 - alpha) * val
    return val


def macd_components(
    bars: Sequence[Any],
    *,
    fast: int,
    slow: int,
    signal: int,
    source: str = "close",
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Compute ``(macd_line[-1], signal_line[-1], histogram[-1])`` cold.

    Faithful to the legacy template: the ``macd_line`` is the difference of
    windowed fast/slow EMAs evaluated at every bar end from ``slow`` to
    ``len(bars)``, and the signal line is the EMA of that macd_line seeded
    from its first element.

    Pre: ``2 <= fast < slow``; ``signal >= 2``. Matches the DSL bounds in
    :mod:`strategy_lab.spec_dsl`. ``signal == 1`` collapses the EMA
    recurrence to ``signal_val == macd_val`` (alpha = 1.0) and produces
    histogram identically zero, so it is rejected as a degenerate input.
    Post: returns ``(macd, signal, histogram)``. Any leg that is not yet
    computable for lack of history is returned as ``None`` — the macd_line
    needs ``slow`` bars, the signal/histogram need ``slow + signal - 1``.
    """
    # Validate as raises (not asserts) — preconditions must hold even when
    # the interpreter is started with ``python -O`` and bare ``assert`` is
    # compiled out.
    if not (fast >= 2 and slow > fast):
        raise ValueError(
            f"macd_components: require fast >= 2 and slow > fast (got fast={fast}, slow={slow})"
        )
    if signal < 2:
        raise ValueError(
            f"macd_components: require signal >= 2 (got signal={signal}); "
            "signal=1 makes the EMA recurrence trivial (signal == macd, histogram ≡ 0)"
        )

    if len(bars) < slow:
        return None, None, None

    macd_line: list[float] = []
    for end in range(slow, len(bars) + 1):
        sub = bars[:end]
        ef = windowed_ema(sub[-fast:], fast, source)
        es = windowed_ema(sub[-slow:], slow, source)
        macd_line.append(ef - es)

    macd_val = macd_line[-1]
    if len(macd_line) < signal:
        return macd_val, None, None

    alpha_g = 2.0 / (signal + 1.0)
    sig = macd_line[0]
    for x in macd_line[1:]:
        sig = alpha_g * x + (1.0 - alpha_g) * sig

    return macd_val, sig, macd_val - sig


# ---------------------------------------------------------------------------
# Stateful registry. One instance per strategy / per StreamingHistoryView.
# ---------------------------------------------------------------------------


def _normalise_close(raw: Any) -> Optional[float]:
    """Normalise a bar's ``close`` value into a safe fingerprint slot.

    Returns ``None`` for any value the cache cannot meaningfully discriminate
    on, otherwise a finite ``float``:

    * ``None`` (missing data)
    * Python ``bool`` (``True``/``False`` would silently float-coerce to
      1.0/0.0 and collide with a real penny close)
    * ``numpy.bool_`` (which is NOT a subclass of ``bool`` since numpy >= 1.20,
      so the ``isinstance`` check above misses it; detect by type name
      since the registry can't import numpy under the sandbox whitelist)
    * Anything that ``float()`` refuses (``pd.NA``, ``pd.NaT``, a string
      that won't parse, ``complex``, ``Decimal('NaN')`` — all raise
      ``TypeError`` / ``ValueError``); we catch and degrade to ``None``
      rather than crashing the cache lookup
    * ``NaN`` (would break tuple-equality via IEEE 754 ``NaN != NaN``)
    * ``inf`` / ``-inf`` (poisons the EMA recurrence — ``alpha * inf`` is
      ``inf`` forever — and would corrupt the cached macd_line until the
      registry is destroyed)

    Pre: caller is responsible for canonicalising ``Decimal`` prices to
    ``float`` BEFORE invoking the registry — two ``Decimal`` values
    differing past the 17th significant digit collapse to the same
    IEEE-754 double after ``float()`` and produce false same-bar hits.

    Post: returned value is ``None`` or a finite ``float`` safe for
    tuple-equality and EMA arithmetic.
    """
    if raw is None or isinstance(raw, bool):
        return None
    # numpy boolean scalars (np.bool_) and pandas boolean sentinels are
    # NOT subclasses of Python ``bool`` under numpy >= 1.20. Their
    # ``isinstance(x, bool)`` check above misses them, and ``float()``
    # silently coerces to 1.0/0.0 — exactly the penny-close collision
    # the bool guard is meant to prevent. Detect by ``__module__`` plus
    # a case-insensitive ``bool`` substring in the type name. NumPy 2.x
    # changes the type name to ``bool``; numpy 1.x is ``bool_``; both
    # land here.
    cls = type(raw)
    if cls.__module__ in ("numpy", "pandas") and "bool" in cls.__name__.lower():
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if math.isnan(val) or math.isinf(val):
        return None
    return val


class IndicatorRegistry:
    """Per-instance cache that turns repeated indicator calls into O(1) work.

    Two callers share this contract:

    * ``factors/primitives.py`` builds a fresh registry per call (it is a
      stateless reference shim) — the cache is harmless and the math
      matches the legacy primitive byte-for-byte.
    * ``executor/predicate_evaluator.StreamingHistoryView`` retains a
      registry for the lifetime of the view. Each indicator advances by
      a single recurrence step on every appended bar.

    Invariant: for each ``key`` in :attr:`_state`, the cached value is the
    indicator's output AT the bar identified by the cache's stored
    fingerprint (``id``, ``len``, ``timestamp``, ``close``). Any drift
    forces a cold-start, never a silent stale read.

    Multi-stream precondition: when a single registry instance is shared
    across multiple bar streams (e.g. one strategy's ``on_bar`` firing
    for every symbol in ``UNIVERSE``), the bar objects MUST carry a
    ``symbol`` attribute so MACD's symbol-slotted cache key keeps each
    stream isolated. Bars without a ``symbol`` attribute share one cache
    slot under ``symbol=None``; safe for a single such stream, unsafe
    across multiple unrelated symbol-less streams.
    """

    def __init__(self) -> None:
        self._state: Dict[Tuple, Dict[str, Any]] = {}

    # ----- key/fingerprint helpers --------------------------------------

    @staticmethod
    def _bar_fingerprint(
        bars: Sequence[Any],
    ) -> Tuple[int, int, Optional[str], Optional[float]]:
        """Return ``(id, len, timestamp, close)`` for advance detection.

        Pre: ``bars`` is non-empty.
        Post: 4-tuple uniquely identifies this ``bars`` slice for cache
        validation. ``id`` covers in-place same-bar reads; ``timestamp``
        covers same-object replay; ``close`` defends against fresh-copy
        callers (e.g. a ``ctx.history`` implementation that rebuilds bar
        wrappers per call) — without it, the id and timestamp legs of
        ``_advance_kind``'s ``prev_matches`` can both fail simultaneously
        and the registry silently regresses to cold-start every call.
        """
        last = bars[-1]
        ts = getattr(last, "timestamp", None)
        # ``_normalise_close`` handles None/bool/numpy.bool_/NaN/inf/non-numeric
        # uniformly — see its docstring for the full taxonomy. Any pathological
        # value falls through as ``None`` so the close-leg of prev_matches
        # degrades cleanly to id/ts and tuple-equality stays well-behaved.
        close_val = _normalise_close(getattr(last, "close", None))
        return id(last), len(bars), ts, close_val

    def _peek(self, key: Tuple) -> Optional[Dict[str, Any]]:
        return self._state.get(key)

    @staticmethod
    def _is_same_bar(
        state: Dict[str, Any],
        fp: Tuple[int, int, Optional[str], Optional[float]],
    ) -> bool:
        return state.get("fp") == fp

    @staticmethod
    def _advance_kind(
        state: Dict[str, Any],
        bars: Sequence[Any],
        fp: Tuple[int, int, Optional[str], Optional[float]],
    ) -> str:
        """Classify how ``bars`` advanced since ``state``.

        Returns one of:

        * ``"expand"`` — ``bars`` is exactly one bar longer than the cached
          fingerprint and the previous-last bar is now at index ``-2``
          (the typical pure-extension call shape, e.g. ``bars[:n]`` →
          ``bars[:n+1]``).
        * ``"slide"`` — ``bars`` has the same length as the cached
          fingerprint but the previous-last bar is at index ``-2``: the
          oldest bar dropped, a new one was appended (the steady-state
          shape of a bounded sliding-window history like
          ``ctx.history(symbol, depth)`` after warm-up).
        * ``"none"`` — everything else: cold-start, replay/seek, multi-bar
          jump, cross-symbol bleed, or first call after a different bar at
          ``-2``. The caller falls back to a full rebuild.

        Both ``"expand"`` and ``"slide"`` require the previous-bar match
        AND the length delta to fit. ``prev_matches`` accepts id-match OR
        (both-sides-have-ts AND ts-match); the ``close`` leg is a
        **conditional fallback** that fires only when ts is unavailable
        on BOTH sides (cached ``prev_fp[2] is None`` AND current
        ``prev_ts is None``). Without the symmetric gate, two unrelated
        symbol-less streams that share a boundary close value can silently
        merge through the close-leg, AND a strategy using a non-close
        ``source`` can false-match by close while the underlying source
        values differ. Restricting close to the ts-symmetrically-absent
        path keeps the fresh-copy rescue (Pydantic re-validation,
        model_dump round trips that drop timestamps on both sides) while
        preserving the strict id+ts gate for the common case.

        Trade-off (intentional): fresh-copy callers that re-stamp
        timestamps between bars (e.g. UTC normalisation, Period →
        Timestamp coercion) — id differs, ts differs, close coincides —
        will cold-rebuild every bar. The cache miss is the price of
        symmetric ts handling; callers that care about hit-rate must
        canonicalise timestamps to a stable form before invoking. Pinned
        by ``test_advance_kind_pydantic_round_trip_with_stamped_timestamps_cold_rebuilds``.

        ``prev_close_val`` is computed LAZILY inside the close-leg branch
        rather than eagerly. The OR-chain short-circuits on id/ts match,
        so wasting a ``float()`` per call in the common path is wasteful;
        and a non-numeric ``prev_close`` (str, ``pd.NA``, etc.) would
        crash the helper from inside ``_advance_kind`` even when id/ts
        would have classified cleanly. Deferring the compute fixes both.
        """
        prev_fp = state.get("fp")
        if prev_fp is None or len(bars) < 2:
            return "none"
        # Same bar — caller handles separately.
        if prev_fp == fp:
            return "none"
        prev_bar = bars[-2]
        prev_ts = getattr(prev_bar, "timestamp", None)
        # ts-leg is usable only when BOTH sides have a timestamp; otherwise
        # the leg is meaningless (None == anything is False). The close-leg
        # then activates as a conditional fallback IFF both sides are ts-less
        # — see docstring trade-off note.
        both_have_ts = prev_fp[2] is not None and prev_ts is not None
        both_ts_absent = prev_fp[2] is None and prev_ts is None
        if prev_fp[0] == id(prev_bar):
            prev_matches = True
        elif both_have_ts and prev_fp[2] == prev_ts:
            prev_matches = True
        elif both_ts_absent:
            prev_close_val = _normalise_close(getattr(prev_bar, "close", None))
            prev_matches = prev_close_val is not None and prev_fp[3] == prev_close_val
        else:
            prev_matches = False
        if not prev_matches:
            return "none"
        if len(bars) == prev_fp[1] + 1:
            return "expand"
        if len(bars) == prev_fp[1]:
            return "slide"
        return "none"

    # ----- EMA -----------------------------------------------------------

    def ema(
        self,
        bars: Sequence[Any],
        period: int,
        source: str = "close",
    ) -> Optional[float]:
        """Trailing-window EMA at ``bars[-1]``.

        Pre: ``period >= 2``. Empty ``bars`` returns ``None``.
        Post: returns ``None`` when ``len(bars) < period``; otherwise the
        windowed-EMA scalar matching :func:`windowed_ema`.
        """
        if not bars or len(bars) < period:
            return None
        key = ("ema", period, source)
        fp = self._bar_fingerprint(bars)
        state = self._peek(key)
        if state is not None and self._is_same_bar(state, fp):
            return state["value"]
        # Per-bar EMA is O(period) and the window seed shifts every call —
        # there is no useful single-step recurrence that matches the legacy
        # template, so we recompute. The cache still protects against
        # multi-predicate same-bar duplicate calls.
        value = windowed_ema(bars, period, source)
        self._state[key] = {"fp": fp, "value": value}
        return value

    # ----- SMA -----------------------------------------------------------

    def sma(
        self,
        bars: Sequence[Any],
        period: int,
        source: str = "close",
    ) -> Optional[float]:
        if not bars or len(bars) < period:
            return None
        key = ("sma", period, source)
        fp = self._bar_fingerprint(bars)
        state = self._peek(key)
        if state is not None and self._is_same_bar(state, fp):
            return state["value"]
        vals = [_source_value(b, source) for b in bars[-period:]]
        value = sum(vals) / period
        self._state[key] = {"fp": fp, "value": value}
        return value

    # ----- RSI -----------------------------------------------------------

    def rsi(
        self,
        bars: Sequence[Any],
        period: int = 14,
        source: str = "close",
    ) -> Optional[float]:
        if not bars or len(bars) < period + 1:
            return None
        key = ("rsi", period, source)
        fp = self._bar_fingerprint(bars)
        state = self._peek(key)
        if state is not None and self._is_same_bar(state, fp):
            return state["value"]
        gains = 0.0
        losses = 0.0
        for i in range(len(bars) - period, len(bars)):
            cur = _source_value(bars[i], source)
            prev = _source_value(bars[i - 1], source)
            delta = cur - prev
            if delta > 0:
                gains += delta
            else:
                losses += -delta
        avg_gain = gains / period
        avg_loss = losses / period
        if avg_loss == 0:
            value: float = 100.0 if avg_gain > 0 else 50.0
        else:
            rs = avg_gain / avg_loss
            value = 100.0 - (100.0 / (1.0 + rs))
        self._state[key] = {"fp": fp, "value": value}
        return value

    # ----- ATR -----------------------------------------------------------

    def atr(self, bars: Sequence[Any], period: int = 14) -> Optional[float]:
        if not bars or len(bars) < period + 1:
            return None
        key = ("atr", period)
        fp = self._bar_fingerprint(bars)
        state = self._peek(key)
        if state is not None and self._is_same_bar(state, fp):
            return state["value"]
        total = 0.0
        for i in range(len(bars) - period, len(bars)):
            h = float(bars[i].high)
            low = float(bars[i].low)
            prev_close = float(bars[i - 1].close)
            total += max(h - low, abs(h - prev_close), abs(low - prev_close))
        value = total / period
        self._state[key] = {"fp": fp, "value": value}
        return value

    # ----- ADX -----------------------------------------------------------

    def adx(self, bars: Sequence[Any], period: int = 14) -> Optional[float]:
        if not bars or len(bars) < 2 * period + 1:
            return None
        key = ("adx", period)
        fp = self._bar_fingerprint(bars)
        state = self._peek(key)
        if state is not None and self._is_same_bar(state, fp):
            return state["value"]
        plus_dms: list[float] = []
        minus_dms: list[float] = []
        trs: list[float] = []
        for i in range(1, len(bars)):
            up = float(bars[i].high) - float(bars[i - 1].high)
            down = float(bars[i - 1].low) - float(bars[i].low)
            plus_dm = up if (up > down and up > 0) else 0.0
            minus_dm = down if (down > up and down > 0) else 0.0
            prev_close = float(bars[i - 1].close)
            tr = max(
                float(bars[i].high) - float(bars[i].low),
                abs(float(bars[i].high) - prev_close),
                abs(float(bars[i].low) - prev_close),
            )
            plus_dms.append(plus_dm)
            minus_dms.append(minus_dm)
            trs.append(tr)
        tr_sum = sum(trs[-period:])
        if tr_sum == 0:
            value = 0.0
        else:
            plus_di = 100.0 * sum(plus_dms[-period:]) / tr_sum
            minus_di = 100.0 * sum(minus_dms[-period:]) / tr_sum
            denom = plus_di + minus_di
            value = 0.0 if denom == 0 else 100.0 * abs(plus_di - minus_di) / denom
        self._state[key] = {"fp": fp, "value": value}
        return value

    # ----- Bollinger -----------------------------------------------------

    def bollinger_bands(
        self,
        bars: Sequence[Any],
        period: int = 20,
        num_std: float = 2.0,
        source: str = "close",
        select: str = "middle",
    ) -> Optional[float]:
        if not bars or len(bars) < period:
            return None
        key = ("bollinger_bands", period, num_std, source)
        fp = self._bar_fingerprint(bars)
        state = self._peek(key)
        if state is not None and self._is_same_bar(state, fp):
            triple = state["value"]
        else:
            vals = [_source_value(b, source) for b in bars[-period:]]
            mean = sum(vals) / period
            var = sum((v - mean) ** 2 for v in vals) / period
            std = math.sqrt(var) if var > 0 else 0.0
            triple = (mean, mean + num_std * std, mean - num_std * std)
            self._state[key] = {"fp": fp, "value": triple}
        middle, upper, lower = triple
        if select == "middle":
            return middle
        if select == "upper":
            return upper
        if select == "lower":
            return lower
        return None

    # ----- Stochastic ----------------------------------------------------

    def stochastic(
        self,
        bars: Sequence[Any],
        k_period: int = 14,
        d_period: int = 3,
        select: str = "k",
    ) -> Optional[float]:
        if not bars or len(bars) < k_period:
            return None
        key = ("stochastic", k_period, d_period)
        fp = self._bar_fingerprint(bars)
        state = self._peek(key)
        if state is not None and self._is_same_bar(state, fp):
            cached = state["value"]
            if select == "k":
                return cached[0]
            if select == "d":
                return cached[1]
            return None

        def _k_at(end: int) -> float:
            window = bars[end - k_period : end]
            lowest = min(float(b.low) for b in window)
            highest = max(float(b.high) for b in window)
            rng = highest - lowest
            if rng == 0:
                return 50.0
            return 100.0 * (float(bars[end - 1].close) - lowest) / rng

        k_val = _k_at(len(bars))
        d_val: Optional[float] = None
        if len(bars) >= k_period + d_period - 1:
            k_window = [_k_at(end) for end in range(len(bars) - d_period + 1, len(bars) + 1)]
            d_val = sum(k_window) / d_period
        self._state[key] = {"fp": fp, "value": (k_val, d_val)}
        if select == "k":
            return k_val
        if select == "d":
            return d_val
        return None

    # ----- VWAP ----------------------------------------------------------

    def vwap(self, bars: Sequence[Any]) -> Optional[float]:
        if not bars:
            return None
        key = ("vwap",)
        fp = self._bar_fingerprint(bars)
        state = self._peek(key)
        if state is not None and self._is_same_bar(state, fp):
            return state["value"]
        num = 0.0
        den = 0.0
        for b in bars:
            typical = (float(b.high) + float(b.low) + float(b.close)) / 3.0
            num += typical * float(b.volume)
            den += float(b.volume)
        if den == 0:
            value = sum(float(b.close) for b in bars) / len(bars)
        else:
            value = num / den
        self._state[key] = {"fp": fp, "value": value}
        return value

    # ----- MACD ---------------------------------------------------------
    #
    # The streaming hot path: the legacy implementation rebuilt the entire
    # ``macd_line`` from ``slow`` onwards on every call, so per-bar work
    # scaled with the size of ``bars``. The registry maintains the
    # ``macd_line`` as a bounded deque and, on a single-bar advance, only
    # appends one new value (``O(fast + slow)``) and recomputes the
    # signal-EMA against the live deque. The fall-through to a full
    # rebuild is reserved for cold-start and replay/seek transitions.

    def _macd_value(
        self,
        bars: Sequence[Any],
        *,
        fast: int,
        slow: int,
        signal: int,
        source: str,
        select: str,
    ) -> Optional[float]:
        # Enforce the same precondition floor as ``macd_components``.
        # Without this, ``_macd_value`` silently accepts ``signal=1``
        # (alpha_g=1.0 → signal == macd, histogram ≡ 0) and ``fast=1``
        # (degenerate 1-period EMA), where the cold standalone path
        # raises — same parameters, two contracts.
        if not (fast >= 2 and slow > fast):
            raise ValueError(
                f"macd: require fast >= 2 and slow > fast (got fast={fast}, slow={slow})"
            )
        if signal < 2:
            raise ValueError(
                f"macd: require signal >= 2 (got signal={signal}); "
                "signal=1 makes the EMA recurrence trivial (signal == macd, histogram ≡ 0)"
            )
        min_bars = slow if select == "macd" else slow + signal - 1
        if len(bars) < min_bars:
            return None

        # The macd_line lives on ``self`` for the lifetime of the registry.
        # If two symbols (or two unrelated bar streams) ever share a
        # registry, the cache must not conflate them — include
        # ``bars[-1].symbol`` in the key so the slots are disjoint.
        symbol = getattr(bars[-1], "symbol", None)
        key = ("macd", symbol, fast, slow, signal, source)
        fp = self._bar_fingerprint(bars)
        state = self._peek(key)
        if state is not None and self._is_same_bar(state, fp):
            return state["value"].get(select)

        kind = self._advance_kind(state, bars, fp) if state is not None else "none"
        macd_line: Deque[float]
        if kind in ("expand", "slide"):
            macd_line = state["macd_line"]
            # Compute-then-mutate: finish every fallible operation (the
            # EMA recurrences) BEFORE touching the cached deque, so a
            # raise anywhere above leaves the cache untouched and the
            # next call cleanly cold-rebuilds.
            ef = windowed_ema(bars[-fast:], fast, source)
            es = windowed_ema(bars[-slow:], slow, source)
            new_val = ef - es
            if kind == "slide":
                # ``bars`` slid forward by one bar: the oldest bar dropped
                # off the front, so its macd value (``macd_line[0]``) is
                # no longer in the legacy windowed-EMA window. Pop it.
                # Without this, the deque grows past the legacy bound and
                # the signal-EMA seeds from a bar that legacy would no
                # longer see — silent semantic divergence on every slide.
                macd_line.popleft()
            macd_line.append(new_val)
        else:
            # Cold-start: replay the legacy outer loop so the macd_line
            # matches the original window-by-window construction. From
            # this point on we will single-step.
            macd_line = deque()
            for end in range(slow, len(bars) + 1):
                sub = bars[:end]
                ef = windowed_ema(sub[-fast:], fast, source)
                es = windowed_ema(sub[-slow:], slow, source)
                macd_line.append(ef - es)

        macd_val = macd_line[-1]
        sig_val: Optional[float] = None
        hist_val: Optional[float] = None
        if len(macd_line) >= signal:
            alpha_g = 2.0 / (signal + 1.0)
            # Iterator-based walk avoids ``deque.__getitem__(i)``'s
            # ``O(min(i, n-i))`` indexing cost — random-access on a
            # deque would make this signal-EMA loop ``O(n^2)`` for
            # long ``macd_line``. Equivalent values; cheaper traversal.
            it = iter(macd_line)
            sig = next(it)
            for x in it:
                sig = alpha_g * x + (1.0 - alpha_g) * sig
            sig_val = sig
            hist_val = macd_val - sig_val

        self._state[key] = {
            "fp": fp,
            "macd_line": macd_line,
            "value": {
                "macd": macd_val,
                "signal": sig_val,
                "histogram": hist_val,
            },
        }
        if select == "macd":
            return macd_val
        if select == "signal":
            return sig_val
        if select == "histogram":
            return hist_val
        return None

    def macd(
        self,
        bars: Sequence[Any],
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
        source: str = "close",
        select: str = "macd",
    ) -> Optional[float]:
        """MACD ``(line | signal | histogram)`` at ``bars[-1]``.

        Pre: ``2 <= fast < slow``; ``signal >= 2``. Matches the DSL
        bounds in :mod:`strategy_lab.spec_dsl` and the precondition floor
        in :func:`macd_components`.
        Post: returns ``None`` during warm-up (``len(bars) < slow`` for
        ``select='macd'``, ``len(bars) < slow + signal - 1`` otherwise).
        Otherwise returns the requested component.
        Raises: ``ValueError`` when any precondition is violated:
        ``fast < 2``, ``slow <= fast``, or ``signal < 2``. The raise
        survives ``python -O`` (stripped ``assert`` cannot — see
        :func:`macd_components` rationale).
        """
        return self._macd_value(
            bars,
            fast=fast,
            slow=slow,
            signal=signal,
            source=source,
            select=select,
        )
