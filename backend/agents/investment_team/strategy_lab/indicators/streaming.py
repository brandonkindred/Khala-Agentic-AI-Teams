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

    Pre: ``2 <= fast < slow``; ``signal >= 1``.
    Post: returns ``(macd, signal, histogram)``. Any leg that is not yet
    computable for lack of history is returned as ``None`` — the macd_line
    needs ``slow`` bars, the signal/histogram need ``slow + signal - 1``.
    """
    # Validate as raises (not asserts) — preconditions must hold even when
    # the interpreter is started with ``python -O`` and bare ``assert`` is
    # compiled out.
    if not (fast >= 1 and slow > fast):
        raise ValueError(
            f"macd_components: require fast >= 1 and slow > fast (got fast={fast}, slow={slow})"
        )
    if signal < 1:
        raise ValueError(f"macd_components: require signal >= 1 (got signal={signal})")

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
    ``last_id`` / ``last_ts`` / ``last_len`` triple. Any drift forces a
    cold-start, never a silent stale read.
    """

    def __init__(self) -> None:
        self._state: Dict[Tuple, Dict[str, Any]] = {}

    # ----- key/fingerprint helpers --------------------------------------

    @staticmethod
    def _bar_fingerprint(bars: Sequence[Any]) -> Tuple[int, int, Optional[str]]:
        """Return ``(id(last_bar), len(bars), last_ts)`` for advance detection.

        Pre: ``bars`` is non-empty.
        Post: tuple uniquely identifies this exact ``bars`` slice for cache
        validation. ``id`` defends against same-bar repeat calls;
        ``timestamp`` defends against truncation that happens to land on
        the same memory address.
        """
        last = bars[-1]
        ts = getattr(last, "timestamp", None)
        return id(last), len(bars), ts

    def _peek(self, key: Tuple) -> Optional[Dict[str, Any]]:
        return self._state.get(key)

    @staticmethod
    def _is_same_bar(state: Dict[str, Any], fp: Tuple[int, int, Optional[str]]) -> bool:
        return state.get("fp") == fp

    @staticmethod
    def _advance_kind(
        state: Dict[str, Any],
        bars: Sequence[Any],
        fp: Tuple[int, int, Optional[str]],
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
        AND the length delta to fit. Earlier revisions accepted any
        previous-bar match (id OR timestamp) with no length check, which
        let cross-symbol or jump-and-replay scenarios slip through as
        "single-step" and silently corrupt the cached ``macd_line``.
        """
        prev_fp = state.get("fp")
        if prev_fp is None or len(bars) < 2:
            return "none"
        # Same bar — caller handles separately.
        if prev_fp == fp:
            return "none"
        prev_bar = bars[-2]
        prev_ts = getattr(prev_bar, "timestamp", None)
        prev_matches = (prev_fp[0] == id(prev_bar)) or (
            prev_ts is not None and prev_fp[2] == prev_ts
        )
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
        if fast >= slow:
            return None
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
            ef = windowed_ema(bars[-fast:], fast, source)
            es = windowed_ema(bars[-slow:], slow, source)
            if kind == "slide":
                # ``bars`` slid forward by one bar: the oldest bar dropped
                # off the front, so its macd value (``macd_line[0]``) is
                # no longer in the legacy windowed-EMA window. Pop it.
                # Without this, the deque grows past the legacy bound and
                # the signal-EMA seeds from a bar that legacy would no
                # longer see — silent semantic divergence on every slide.
                macd_line.popleft()
            macd_line.append(ef - es)
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
            sig = macd_line[0]
            for x_idx in range(1, len(macd_line)):
                sig = alpha_g * macd_line[x_idx] + (1.0 - alpha_g) * sig
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

        Pre: ``2 <= fast < slow``; ``signal >= 1``.
        Post: returns ``None`` during warm-up (``len(bars) < slow`` for
        ``select='macd'``, ``len(bars) < slow + signal - 1`` otherwise) or
        when ``fast >= slow``. Otherwise returns the requested component.
        """
        return self._macd_value(
            bars,
            fast=fast,
            slow=slow,
            signal=signal,
            source=source,
            select=select,
        )
