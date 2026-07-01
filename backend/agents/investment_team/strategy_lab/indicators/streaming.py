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
    # ``not (x >= y)`` rather than ``x < y`` so NaN-typed parameters trip the
    # gate (NaN is unordered with everything under IEEE 754; ``NaN < 2`` is
    # False — a strict ``<`` check would silently admit NaN and poison the
    # macd_line recurrence). Matches ``IndicatorRegistry._macd_value``.
    if not (fast >= 2) or not (slow > fast):
        raise ValueError(
            f"macd_components: require fast >= 2 and slow > fast (got fast={fast}, slow={slow})"
        )
    if not (signal >= 2):
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
    * Third-party boolean scalars (``numpy.bool_`` in numpy 1.x or
      ``numpy.bool`` in numpy 2.x, ``numpy.ma.bool_``, pandas
      ``BooleanScalar``, ``pyarrow.BooleanScalar``, Polars boolean
      scalars). Detected by ``(top-level module, exact type-name
      allowlist)`` — broader than the Python ``isinstance(x, bool)``
      check, which misses these because they are NOT subclasses of
      Python ``bool``.
    * Anything that ``float()`` refuses (``pd.NA``, ``pd.NaT``, a string
      that won't parse, ``complex``) — raises ``TypeError`` /
      ``ValueError`` / ``OverflowError`` (astronomical-magnitude ints).
      All three are caught and degrade to ``None`` rather than crashing
      the cache lookup.
    * ``NaN`` (would break tuple-equality via IEEE 754 ``NaN != NaN``).
      Note: ``float(Decimal('NaN'))`` returns ``nan`` WITHOUT raising —
      it's caught here at the ``math.isnan`` gate, NOT by the except.
    * ``inf`` / ``-inf`` (poisons the EMA recurrence — ``alpha * inf`` is
      ``inf`` forever — and would corrupt the cached macd_line until the
      registry is destroyed).

    Pre: caller is responsible for canonicalising ``Decimal`` prices to
    ``float`` BEFORE invoking the registry — two ``Decimal`` values
    differing past the 17th significant digit collapse to the same
    IEEE-754 double after ``float()`` and produce false same-bar hits.

    Post: returned value is ``None`` or a finite ``float`` safe for
    tuple-equality and EMA arithmetic.

    Note (DbC): this helper silently degrades the fingerprint slot to
    ``None`` for pathological closes; the loud-fail signal for
    non-numeric closes still surfaces downstream inside
    :func:`windowed_ema` (which calls ``float(bar.close)`` directly and
    propagates the ``TypeError`` from ``pd.NA``, etc). The cache layer
    is intentionally lenient; the indicator-math layer remains strict.
    """
    if raw is None or isinstance(raw, bool):
        return None
    # Third-party boolean scalars are NOT subclasses of Python ``bool``
    # under numpy >= 1.20, pandas Boolean dtype, PyArrow, Polars, etc.
    # ``isinstance(x, bool)`` above misses them, and ``float()`` would
    # silently coerce to 1.0/0.0 — the penny-close collision the guard
    # exists to prevent. Detect by (top-level module, exact type-name
    # allowlist) so:
    #   * numpy submodules (``numpy.ma.core``, ``numpy.dtypes``) are
    #     covered via ``split('.')[0] == 'numpy'``.
    #   * pyarrow's ``pyarrow.lib`` and Polars' ``polars`` get the same
    #     handling.
    #   * Substring matches (e.g. ``BooleanIndex``, ``BoolDtype``) are
    #     rejected by the exact-name allowlist, which only catches
    #     genuine scalar booleans.
    cls = type(raw)
    # ``cls.__module__`` is normally a string but can be ``None`` for
    # dynamically-created classes (``type()``-built without a module) or
    # exotic C-extension types. Guard explicitly so the lenient-by-design
    # cache helper doesn't crash on AttributeError before the float() gate.
    module_name = getattr(cls, "__module__", None)
    type_name = getattr(cls, "__name__", "")
    if isinstance(module_name, str) and isinstance(type_name, str):
        top_level = module_name.split(".", 1)[0]
        if top_level in ("numpy", "pandas", "pyarrow", "polars") and type_name.lower() in (
            "bool",
            "bool_",
            "boolean",
            "booleanscalar",
            "boolscalar",
            "bool8",
        ):
            return None
    try:
        val = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if math.isnan(val) or math.isinf(val):
        return None
    return val


_SAFE_GETATTR_EXC: Tuple[type, ...] = (
    AttributeError,
    TypeError,
    ValueError,
    RuntimeError,
    LookupError,
)


def _safe_getattr(bar: Any, name: str) -> Any:
    """Read ``getattr(bar, name, None)`` defensively for cache-layer use.

    ``getattr`` only uses the default when the attribute name doesn't
    resolve. If the attribute is a Python ``@property`` or a Pydantic
    ``computed_field`` whose body raises (lazy DB session, Decimal('NaN')
    intermediate, AttributeError from deeper in the descriptor), the
    exception propagates — that crashed the indicator helpers from
    inside ``_bar_fingerprint``/``_advance_kind`` whenever the close,
    timestamp, or symbol descriptor misbehaved. This wrapper traps the
    documented descriptor-raise classes (``AttributeError`` /
    ``TypeError`` / ``ValueError`` / ``RuntimeError`` / ``LookupError``)
    and degrades to ``None`` so the cache layer remains exception-safe;
    the indicator-math layer (``windowed_ema``) is still strict.

    Programmer/runtime sentinels propagate unchanged:
    ``KeyboardInterrupt`` and ``SystemExit`` because they inherit from
    ``BaseException`` rather than ``Exception``; ``AssertionError``,
    ``MemoryError``, ``RecursionError`` because they are not in the
    catch tuple; and ``NotImplementedError`` because it is re-raised
    explicitly below — even though it inherits from ``RuntimeError``
    (and would otherwise be swallowed), a subclass-override sentinel
    must reach the caller, not silently degrade to ``close=None``.
    """
    try:
        return getattr(bar, name, None)
    except NotImplementedError:
        # Subclass of RuntimeError; would be swallowed by the tuple below.
        # NotImplementedError is a deliberate programmer signal — propagate.
        raise
    except _SAFE_GETATTR_EXC:
        return None


def _safe_read_close(bar: Any) -> Any:
    """Backwards-compatible alias of ``_safe_getattr(bar, 'close')``.

    Retained because the close-read site is the most frequent caller and
    callers in this module read more readably as ``_safe_read_close(bar)``.
    """
    return _safe_getattr(bar, "close")


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
        # All bar attribute reads route through ``_safe_getattr`` so a
        # raising ``@property``/Pydantic ``computed_field`` on ANY of
        # ``timestamp``/``close``/``symbol`` degrades cleanly to ``None``
        # rather than crashing the cache layer. ``_normalise_close``
        # handles None/bool/numpy.bool_/NaN/inf/non-numeric uniformly.
        ts = _safe_getattr(last, "timestamp")
        close_val = _normalise_close(_safe_getattr(last, "close"))
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
        prev_ts = _safe_getattr(prev_bar, "timestamp")
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
            prev_close_val = _normalise_close(_safe_getattr(prev_bar, "close"))
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

    @staticmethod
    def _adx_dm_tr(cur: Any, prev: Any) -> Tuple[float, float, float]:
        """Directional-movement / true-range triple for one consecutive bar pair.

        Pre: ``cur`` is the bar immediately following ``prev`` in the series.
        Post: returns ``(plus_dm, minus_dm, tr)`` for the pair — identical to
        the legacy per-bar loop body, so summing these reproduces the legacy
        ADX bit-for-bit.
        """
        cur_high = float(cur.high)
        cur_low = float(cur.low)
        up = cur_high - float(prev.high)
        down = float(prev.low) - cur_low
        plus_dm = up if (up > down and up > 0) else 0.0
        minus_dm = down if (down > up and down > 0) else 0.0
        prev_close = float(prev.close)
        tr = max(cur_high - cur_low, abs(cur_high - prev_close), abs(cur_low - prev_close))
        return plus_dm, minus_dm, tr

    def adx(self, bars: Sequence[Any], period: int = 14) -> Optional[float]:
        """Average directional index (un-smoothed single-DI form) at ``bars[-1]``.

        Pre: ``period >= 1``. Returns ``None`` until ``len(bars) >= 2*period + 1``.
        Post: the ADX scalar computed from the trailing ``period`` directional-
        movement / true-range triples.

        The value depends only on the last ``period`` ``(plus_dm, minus_dm, tr)``
        triples, so the registry keeps them in a bounded :class:`deque` and
        advances by a single triple per appended bar — O(period) per call,
        independent of how many bars have been seen. The legacy form rebuilt
        every triple from bar 1 (``O(N_bars)`` per call, ``O(N_bars^2)`` per
        backtest); it was the one indicator that still rescanned the full
        history, breaking the registry's "cost independent of history length"
        invariant. Summing the bounded deque oldest-to-newest reproduces the
        legacy ``sum(trs[-period:])`` bit-for-bit, so the value is unchanged.
        """
        if not bars or len(bars) < 2 * period + 1:
            return None
        key = ("adx", period)
        fp = self._bar_fingerprint(bars)
        state = self._peek(key)
        if state is not None and self._is_same_bar(state, fp):
            return state["value"]
        dms: Optional[Deque[Tuple[float, float, float]]] = None
        if state is not None and "dms" in state:
            kind = self._advance_kind(state, bars, fp)
            if kind in ("expand", "slide"):
                # One new bar at the tail: append its triple. The bounded deque
                # evicts the oldest, so it always holds exactly the trailing
                # ``period`` triples (== ``trs[-period:]`` in the legacy form).
                dms = state["dms"]
                dms.append(self._adx_dm_tr(bars[-1], bars[-2]))
        if dms is None:
            # Cold-start / replay / multi-bar jump: rebuild the trailing window.
            dms = deque(maxlen=period)
            for i in range(len(bars) - period, len(bars)):
                dms.append(self._adx_dm_tr(bars[i], bars[i - 1]))
        tr_sum = sum(t[2] for t in dms)
        if tr_sum == 0:
            value = 0.0
        else:
            plus_di = 100.0 * sum(t[0] for t in dms) / tr_sum
            minus_di = 100.0 * sum(t[1] for t in dms) / tr_sum
            denom = plus_di + minus_di
            value = 0.0 if denom == 0 else 100.0 * abs(plus_di - minus_di) / denom
        self._state[key] = {"fp": fp, "value": value, "dms": dms}
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
        """Bollinger Bands at ``bars[-1]``.

        Pre: ``period >= 1``. Returns ``None`` until ``len(bars) >= period``.
        Pre: callers advancing one bar at a time get O(1) warm updates;
        multi-bar jumps are safe — ``_advance_kind`` returns ``"none"`` and
        the state is rebuilt from scratch rather than corrupted.
        Post: the (middle, upper, lower) triple for the trailing ``period``
        bars; the requested ``select`` band is returned as a scalar.

        The bands depend only on the last ``period`` source values, so the
        registry maintains a bounded :class:`deque` of those values together
        with a running sum and running sum-of-squares. On each single-bar
        advance the evicted value is subtracted and the new value is added in
        O(1) time. Population variance is computed as
        ``sum_sq / period − mean²``; the synthesis compiler's
        ``bollinger_bands`` template uses the same formula to keep the two in
        lockstep.

        Postcondition (numerical): ``max(0.0, var)`` guards against tiny
        negative FP residuals from the ``sum_sq/period − mean²`` identity
        when all window values are nearly equal.
        """
        if not bars or len(bars) < period:
            return None
        key = ("bollinger_bands", period, num_std, source)
        fp = self._bar_fingerprint(bars)
        state = self._peek(key)
        if state is not None and self._is_same_bar(state, fp):
            triple = state["value"]
        else:
            vals: Optional[Deque[float]] = None
            s = 0.0
            sq = 0.0
            if state is not None and "vals" in state:
                kind = self._advance_kind(state, bars, fp)
                if kind in ("expand", "slide"):
                    vals = state["vals"]
                    s = state["s"]
                    sq = state["sq"]
                    # The deque is always at full capacity (period values)
                    # once the cold-start gate passes. The outgoing element
                    # (vals[0]) must be removed from the running totals
                    # before the deque evicts it on append.
                    outgoing = vals[0]
                    s -= outgoing
                    sq -= outgoing * outgoing
                    new_v = _source_value(bars[-1], source)
                    vals.append(new_v)
                    s += new_v
                    sq += new_v * new_v
            if vals is None:
                vals = deque(maxlen=period)
                s = 0.0
                sq = 0.0
                for b in bars[-period:]:
                    v = _source_value(b, source)
                    vals.append(v)
                    s += v
                    sq += v * v
            mean = s / period
            var = max(0.0, sq / period - mean * mean)
            std = math.sqrt(var) if var > 0 else 0.0
            triple = (mean, mean + num_std * std, mean - num_std * std)
            self._state[key] = {"fp": fp, "value": triple, "vals": vals, "s": s, "sq": sq}
        middle, upper, lower = triple
        if select == "middle":
            return middle
        if select == "upper":
            return upper
        if select == "lower":
            return lower
        if select == "percent_b":
            # %B locates the live price within the band: 0 at the lower band,
            # 1 at the upper. Flat window (upper == lower) → neutral 0.5 to
            # avoid a 0/0; %B is intentionally unbounded outside [0, 1] when
            # price pierces a band.
            width = upper - lower
            if width == 0:
                return 0.5
            price = _source_value(bars[-1], source)
            return (price - lower) / width
        if select == "bandwidth":
            # Bandwidth normalises the band width by the middle band; 0 when the
            # middle is 0 (degenerate) so the result stays finite.
            if middle == 0:
                return 0.0
            return (upper - lower) / middle
        return None

    # ----- Stochastic ----------------------------------------------------

    def stochastic(
        self,
        bars: Sequence[Any],
        k_period: int = 14,
        d_period: int = 3,
        select: str = "k",
    ) -> Optional[float]:
        """Stochastic oscillator ``(%K | %D)`` at ``bars[-1]``.

        Pre: ``k_period >= 1``; ``d_period >= 1``. Returns ``None`` until
        ``len(bars) >= k_period`` (``select='k'``) or
        ``len(bars) >= k_period + d_period - 1`` (``select='d'``).
        Pre: callers advancing one bar at a time get O(k_period+d_period)
        warm updates; multi-bar jumps are safe — ``_advance_kind`` returns
        ``"none"`` and state is rebuilt from scratch rather than corrupted.
        Post: the %K or %D scalar for the trailing window.

        Two bounded :class:`deque` objects maintain state across calls:

        * ``bars_dq`` (maxlen=``k_period``) — ``(high, low, close)`` triples
          for the trailing ``k_period`` bars. %K is computed from this in
          O(k_period) per call (bounded constant).
        * ``k_dq`` (maxlen=``d_period``) — the last ``d_period`` %K values.
          %D = ``mean(k_dq)`` in O(d_period).

        The previous implementation called the inner ``_k_at`` helper
        ``d_period`` times on every bar, each time slicing the full growing
        ``bars`` list — O(d_period × k_period) per bar, but critically the
        ``range(k_period, len(bars) + 1)`` loop in the compiler template was
        O(len(bars)) and grew unboundedly. Both are now O(k_period + d_period)
        bounded constant regardless of how many bars have been seen.
        """
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

        # Attempt warm-path advance.
        warm = False
        bars_dq: Deque[Tuple[float, float, float]]
        k_dq: Deque[float]
        if state is not None and "bars_dq" in state:
            kind = self._advance_kind(state, bars, fp)
            if kind in ("expand", "slide"):
                warm = True
                bars_dq = state["bars_dq"]
                k_dq = state["k_dq"]
                b = bars[-1]
                bars_dq.append((float(b.high), float(b.low), float(b.close)))

        if not warm:
            # Cold-start: rebuild the k_period bar window and up to d_period
            # %K history values from the minimal required suffix of bars.
            # Iterating only bars[-(k_period + d_period - 1):] keeps the
            # cold-start cost at O(k_period × d_period), not O(len(bars)).
            bars_dq = deque(maxlen=k_period)
            k_dq = deque(maxlen=d_period)
            # We want k_dq to contain the last d_period %K values (all but the
            # current bar). The first position with a full k_period window is
            # at index (k_period - 1) within the suffix we iterate below.
            suffix_start = max(0, len(bars) - k_period - d_period + 1)
            for i in range(suffix_start, len(bars) - 1):
                bt = bars[i]
                bars_dq.append((float(bt.high), float(bt.low), float(bt.close)))
                if len(bars_dq) == k_period:
                    lowest = min(t[1] for t in bars_dq)
                    highest = max(t[0] for t in bars_dq)
                    rng = highest - lowest
                    k = 50.0 if rng == 0 else 100.0 * (bars_dq[-1][2] - lowest) / rng
                    k_dq.append(k)
            # Add the current bar (bars[-1]) to bars_dq.
            b = bars[-1]
            bars_dq.append((float(b.high), float(b.low), float(b.close)))

        # Compute %K for the current trailing window (bars_dq[-1] == bars[-1]).
        lowest = min(t[1] for t in bars_dq)
        highest = max(t[0] for t in bars_dq)
        rng = highest - lowest
        k_val = 50.0 if rng == 0 else 100.0 * (bars_dq[-1][2] - lowest) / rng

        # Append the current %K to the %D history deque.
        k_dq.append(k_val)

        d_val: Optional[float] = None
        if len(k_dq) == d_period:
            d_val = sum(k_dq) / d_period

        self._state[key] = {"fp": fp, "value": (k_val, d_val), "bars_dq": bars_dq, "k_dq": k_dq}
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
        # Two-tier defence:
        #
        # 1. **Type gate.** All three parameters must be ``int`` instances.
        #    A float (e.g. ``fast=2.5``) would pass the value comparison
        #    but blow up downstream on ``bars[-fast:]`` slicing with
        #    ``TypeError: slice indices must be integers``. ``bool`` is
        #    a subclass of ``int``; ``True``/``False`` are admitted by
        #    the type gate and rejected by the value gate (``True >= 2``
        #    is False).
        # 2. **Value gate** using ``not (x >= y)`` rather than ``x < y`` so
        #    NaN-typed parameters trip the gate (NaN is unordered with
        #    everything under IEEE 754, so ``NaN < 2`` is False — a
        #    strict ``<`` check would silently admit NaN and poison the
        #    macd_line recurrence). Note: a float NaN would also pass
        #    the type gate above and fall through; the value gate
        #    catches NaN here. The two gates compose so any malformed
        #    parameter combination — wrong type, NaN, or out-of-range
        #    int — surfaces as ``ValueError``.
        if not (isinstance(fast, int) and isinstance(slow, int) and isinstance(signal, int)):
            raise ValueError(
                f"macd: require integer fast/slow/signal (got types "
                f"fast={type(fast).__name__}, slow={type(slow).__name__}, "
                f"signal={type(signal).__name__})"
            )
        if not (fast >= 2) or not (slow > fast):
            raise ValueError(
                f"macd: require fast >= 2 and slow > fast (got fast={fast}, slow={slow})"
            )
        if not (signal >= 2):
            raise ValueError(
                f"macd: require signal >= 2 (got signal={signal}); "
                "signal=1 makes the EMA recurrence trivial (signal == macd, histogram ≡ 0)"
            )
        # True warm-up gate: need at least ``slow`` bars to compute the
        # first macd_line entry. For ``select='signal'``/``'histogram'``,
        # the signal-EMA only fills at ``len(macd_line) >= signal`` (i.e.
        # ``len(bars) >= slow + signal - 1``) — but we still pass through
        # the body during the warm-up window and write the cache with
        # ``sig_val=None`` so same-bar repeat calls hit the fast path
        # instead of cold-rebuilding. ``select='macd'`` returns a finite
        # value at ``len(bars) == slow`` (matching the synthesis macd
        # template); the factors compiler's MACDSignal helper is a
        # signal-only API and returns NAN until ``signal-EMA fills`` —
        # so the registry and synthesis ``select='macd'`` are MORE
        # permissive than the factors ``MACDSignal`` helper by design
        # (different APIs returning different lines).
        if len(bars) < slow:
            return None

        # The macd_line lives on ``self`` for the lifetime of the registry.
        # If two symbols (or two unrelated bar streams) ever share a
        # registry, the cache must not conflate them — include
        # ``bars[-1].symbol`` in the key so the slots are disjoint.
        # ``_safe_getattr`` traps descriptor raises so a Pydantic
        # computed_field ``symbol`` that misbehaves degrades to ``None``
        # (single-stream behaviour) instead of crashing the call.
        symbol = _safe_getattr(bars[-1], "symbol")
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

        Pre: ``fast``/``slow``/``signal`` are all ``int``;
        ``2 <= fast < slow``; ``signal >= 2``. Matches the DSL bounds in
        :mod:`strategy_lab.spec_dsl` and the precondition floor in
        :func:`macd_components`. Non-int (e.g. float ``2.5``) and
        NaN-typed parameters are both caught — the type gate rejects
        floats outright, and ``not (x >= 2)`` rather than ``x < 2``
        catches NaN ints/floats that pass the type gate.
        Post: returns ``None`` during warm-up (``len(bars) < slow``).
        For ``select='signal'`` / ``'histogram'``, returns ``None`` while
        ``len(bars) < slow + signal - 1`` (signal-EMA hasn't filled),
        but the cache IS written with ``sig_val=None`` / ``hist_val=None``
        so same-bar repeat calls during this sub-window hit the fast path.
        Returns the requested component once history is sufficient.
        Raises: ``ValueError`` when any precondition is violated:
        non-int param, ``fast < 2``, ``slow <= fast``, ``signal < 2``,
        or any of the three is NaN. The raise survives ``python -O``
        (stripped ``assert`` cannot — see :func:`macd_components` rationale).
        """
        return self._macd_value(
            bars,
            fast=fast,
            slow=slow,
            signal=signal,
            source=source,
            select=select,
        )

    # ----- Donchian channels --------------------------------------------

    def donchian(
        self,
        bars: Sequence[Any],
        period: int = 20,
        select: str = "middle",
    ) -> Optional[float]:
        """Donchian channel ``(upper | middle | lower)`` at ``bars[-1]``.

        Pre: ``period >= 1``. Returns ``None`` until ``len(bars) >= period``.
        Post: ``upper`` is the highest high and ``lower`` the lowest low over
        the trailing ``period`` bars; ``middle`` is their midpoint. The bands
        depend only on those ``period`` highs/lows, so the registry keeps a
        bounded :class:`deque` of ``(high, low)`` pairs and recomputes the
        extrema over it — O(period) per call, independent of history length.
        """
        if not bars or len(bars) < period:
            return None
        # If two symbols (or two unrelated bar streams) ever share a registry,
        # the cache must not conflate them — include ``bars[-1].symbol`` in the
        # key so the slots are disjoint, mirroring :meth:`macd`.
        symbol = _safe_getattr(bars[-1], "symbol")
        key = ("donchian", symbol, period)
        fp = self._bar_fingerprint(bars)
        state = self._peek(key)
        if state is not None and self._is_same_bar(state, fp):
            triple = state["value"]
        else:
            hl: Optional[Deque[Tuple[float, float]]] = None
            if state is not None and "hl" in state:
                kind = self._advance_kind(state, bars, fp)
                if kind in ("expand", "slide"):
                    hl = state["hl"]
                    hl.append((float(bars[-1].high), float(bars[-1].low)))
            if hl is None:
                hl = deque(maxlen=period)
                for b in bars[-period:]:
                    hl.append((float(b.high), float(b.low)))
            upper = max(t[0] for t in hl)
            lower = min(t[1] for t in hl)
            triple = (upper, (upper + lower) / 2.0, lower)
            self._state[key] = {"fp": fp, "value": triple, "hl": hl}
        upper, middle, lower = triple
        if select == "upper":
            return upper
        if select == "middle":
            return middle
        if select == "lower":
            return lower
        return None

    # ----- Keltner channels ---------------------------------------------

    def keltner(
        self,
        bars: Sequence[Any],
        period: int = 20,
        atr_period: int = 10,
        multiplier: float = 2.0,
        select: str = "middle",
    ) -> Optional[float]:
        """Keltner channel ``(upper | middle | lower)`` at ``bars[-1]``.

        Pre: ``period >= 1``; ``atr_period >= 1``. Returns ``None`` until
        ``len(bars) >= max(period, atr_period + 1)`` (the ATR leg needs a prior
        close).
        Post: ``middle`` is the windowed close-EMA over ``period`` bars; the
        bands are ``middle ± multiplier × ATR(atr_period)``. Reuses
        :func:`windowed_ema` for the basis and a **simple average of true range**
        for the width — identical to :meth:`atr` above, which is itself an SMA of
        true range (``total / period``), NOT a Wilder/EMA smoothing — so the
        Keltner ATR leg and a standalone ``atr`` indicator return the same value
        and the compiler's inline helper agrees bit-for-bit.

        Unlike the pure-windowed indicators (``donchian``/``williams_r``/``cci``/
        ``mfi``), this method keeps no per-call deque: its dominant cost is the
        shared :func:`windowed_ema` basis, which is inherently O(period) because the
        EMA seed slides with the window and cannot be cached locally without
        changing that shared helper. Caching only the small ``atr_period`` true-range
        leg would not remove that dominant cost, so it is intentionally left plain.
        """
        if not bars or len(bars) < max(period, atr_period + 1):
            return None
        # If two symbols (or two unrelated bar streams) ever share a registry,
        # the cache must not conflate them — include ``bars[-1].symbol`` in the
        # key so the slots are disjoint, mirroring :meth:`macd` and the sibling
        # new indicators (donchian/mfi/cci/williams_r).
        symbol = _safe_getattr(bars[-1], "symbol")
        key = ("keltner", symbol, period, atr_period, multiplier)
        fp = self._bar_fingerprint(bars)
        state = self._peek(key)
        if state is not None and self._is_same_bar(state, fp):
            triple = state["value"]
        else:
            middle = windowed_ema(bars, period, "close")
            total = 0.0
            for i in range(len(bars) - atr_period, len(bars)):
                h = float(bars[i].high)
                low = float(bars[i].low)
                prev_close = float(bars[i - 1].close)
                total += max(h - low, abs(h - prev_close), abs(low - prev_close))
            atr_val = total / atr_period
            triple = (middle + multiplier * atr_val, middle, middle - multiplier * atr_val)
            self._state[key] = {"fp": fp, "value": triple}
        upper, middle, lower = triple
        if select == "upper":
            return upper
        if select == "middle":
            return middle
        if select == "lower":
            return lower
        return None

    # ----- OBV -----------------------------------------------------------

    def obv(self, bars: Sequence[Any]) -> Optional[float]:
        """On-Balance Volume at ``bars[-1]`` (cumulative over ``bars``).

        Pre: ``bars`` is non-empty. Post: the running signed-volume total — add
        ``volume`` when the close rises vs. the prior bar, subtract it when the
        close falls, leave it unchanged on an equal close. Cumulative over the
        whole supplied window (like :meth:`vwap`), so a bounded sliding window
        re-bases OBV to the window start.

        Cost: O(window) per non-same-bar call, matching :meth:`vwap`. An
        incremental update is deliberately NOT used: under the engine's bounded
        ``StreamingHistoryView`` (≤500 bars) every steady-state call is a
        slide whose oldest bar drops, which re-bases the cumulative sum to the
        new window start — so an incremental form would have to recompute anyway,
        exactly as ``vwap`` does. Keeping the two cumulative indicators identical
        outweighs a micro-optimisation that the slide path defeats.
        """
        if not bars:
            return None
        # If two symbols (or two unrelated bar streams) ever share a registry,
        # the cache must not conflate them — include ``bars[-1].symbol`` in the
        # key so the slots are disjoint, mirroring :meth:`macd` and the sibling
        # new indicators (donchian/mfi/cci/williams_r). ``vwap`` predates this
        # convention and stays symbol-less; the new indicators are uniform.
        symbol = _safe_getattr(bars[-1], "symbol")
        key = ("obv", symbol)
        fp = self._bar_fingerprint(bars)
        state = self._peek(key)
        if state is not None and self._is_same_bar(state, fp):
            return state["value"]
        value = 0.0
        for i in range(1, len(bars)):
            cur = float(bars[i].close)
            prev = float(bars[i - 1].close)
            if cur > prev:
                value += float(bars[i].volume)
            elif cur < prev:
                value -= float(bars[i].volume)
        self._state[key] = {"fp": fp, "value": value}
        return value

    # ----- MFI -----------------------------------------------------------

    def mfi(self, bars: Sequence[Any], period: int = 14) -> Optional[float]:
        """Money Flow Index (0–100) at ``bars[-1]``.

        Pre: ``period >= 1``. Returns ``None`` until ``len(bars) >= period + 1``
        (each money-flow term compares typical price against the prior bar).
        Post: the volume-weighted RSI of typical price over the trailing
        ``period`` bars. Mirrors :meth:`rsi`'s zero-denominator convention:
        all-positive flow → 100, no flow at all → 50. Keeps a bounded deque of
        per-bar ``(positive, negative)`` money-flow contributions (like
        :meth:`donchian`) so the warm ``expand``/``slide`` path appends one term
        instead of rewalking the window each call; ``pos``/``neg`` are still summed
        over the deque in oldest-to-newest order, so the value is identical.
        """
        if not bars or len(bars) < period + 1:
            return None
        # If two symbols (or two unrelated bar streams) ever share a registry,
        # the cache must not conflate them — include ``bars[-1].symbol`` in the
        # key so the slots are disjoint, mirroring :meth:`macd`.
        symbol = _safe_getattr(bars[-1], "symbol")
        key = ("mfi", symbol, period)
        fp = self._bar_fingerprint(bars)
        state = self._peek(key)
        if state is not None and self._is_same_bar(state, fp):
            return state["value"]

        def _flow(cur: Any, prev: Any) -> Tuple[float, float]:
            # (positive, negative) raw money flow: the term is signed by whether
            # the typical price rose or fell vs. the prior bar; a flat move is zero.
            tp = (float(cur.high) + float(cur.low) + float(cur.close)) / 3.0
            tp_prev = (float(prev.high) + float(prev.low) + float(prev.close)) / 3.0
            rmf = tp * float(cur.volume)
            if tp > tp_prev:
                return (rmf, 0.0)
            if tp < tp_prev:
                return (0.0, rmf)
            return (0.0, 0.0)

        flows: Optional[Deque[Tuple[float, float]]] = None
        if state is not None and "flows" in state:
            kind = self._advance_kind(state, bars, fp)
            if kind in ("expand", "slide"):
                flows = state["flows"]
                flows.append(_flow(bars[-1], bars[-2]))
        if flows is None:
            flows = deque(maxlen=period)
            for i in range(len(bars) - period, len(bars)):
                flows.append(_flow(bars[i], bars[i - 1]))
        pos = sum(f[0] for f in flows)
        neg = sum(f[1] for f in flows)
        if neg == 0:
            value: float = 100.0 if pos > 0 else 50.0
        else:
            ratio = pos / neg
            value = 100.0 - (100.0 / (1.0 + ratio))
        self._state[key] = {"fp": fp, "value": value, "flows": flows}
        return value

    # ----- ROC -----------------------------------------------------------

    def roc(
        self,
        bars: Sequence[Any],
        period: int = 12,
        source: str = "close",
    ) -> Optional[float]:
        """Rate of Change (percent) at ``bars[-1]`` over ``period`` bars.

        Pre: ``period >= 1``. Returns ``None`` until ``len(bars) >= period + 1``.
        Post: ``100 × (price_now − price_{−period}) / price_{−period}``; ``0.0``
        when the reference price is exactly 0 (avoids a division by zero).
        """
        if not bars or len(bars) < period + 1:
            return None
        # If two symbols (or two unrelated bar streams) ever share a registry,
        # the cache must not conflate them — include ``bars[-1].symbol`` in the
        # key so the slots are disjoint, mirroring :meth:`macd` and the sibling
        # new indicators (donchian/mfi/cci/williams_r).
        symbol = _safe_getattr(bars[-1], "symbol")
        key = ("roc", symbol, period, source)
        fp = self._bar_fingerprint(bars)
        state = self._peek(key)
        if state is not None and self._is_same_bar(state, fp):
            return state["value"]
        cur = _source_value(bars[-1], source)
        prev = _source_value(bars[-1 - period], source)
        value = 0.0 if prev == 0 else (cur - prev) / prev * 100.0
        self._state[key] = {"fp": fp, "value": value}
        return value

    # ----- CCI -----------------------------------------------------------

    def cci(self, bars: Sequence[Any], period: int = 20) -> Optional[float]:
        """Commodity Channel Index at ``bars[-1]``.

        Pre: ``period >= 1``. Returns ``None`` until ``len(bars) >= period``.
        Post: ``(tp − sma_tp) / (0.015 × mean_deviation)`` over the trailing
        ``period`` typical prices, where ``mean_deviation`` is the mean absolute
        deviation from ``sma_tp``; ``0.0`` when that deviation is 0 (a flat
        window has no defined CCI). Keeps a bounded typical-price deque (like
        :meth:`donchian`) so the warm ``expand``/``slide`` path appends one value
        instead of rebuilding the window each call; the SMA and mean-deviation are
        still summed over the deque in oldest-to-newest order, so the value is
        identical (mean absolute deviation has no incremental form — it depends on
        the SMA, which shifts as the window slides).
        """
        if not bars or len(bars) < period:
            return None
        # If two symbols (or two unrelated bar streams) ever share a registry,
        # the cache must not conflate them — include ``bars[-1].symbol`` in the
        # key so the slots are disjoint, mirroring :meth:`macd`.
        symbol = _safe_getattr(bars[-1], "symbol")
        key = ("cci", symbol, period)
        fp = self._bar_fingerprint(bars)
        state = self._peek(key)
        if state is not None and self._is_same_bar(state, fp):
            return state["value"]
        tps: Optional[Deque[float]] = None
        if state is not None and "tps" in state:
            kind = self._advance_kind(state, bars, fp)
            if kind in ("expand", "slide"):
                tps = state["tps"]
                last = bars[-1]
                tps.append((float(last.high) + float(last.low) + float(last.close)) / 3.0)
        if tps is None:
            tps = deque(maxlen=period)
            for b in bars[-period:]:
                tps.append((float(b.high) + float(b.low) + float(b.close)) / 3.0)
        sma_tp = sum(tps) / period
        mean_dev = sum(abs(t - sma_tp) for t in tps) / period
        cur_tp = tps[-1]
        value = 0.0 if mean_dev == 0 else (cur_tp - sma_tp) / (0.015 * mean_dev)
        self._state[key] = {"fp": fp, "value": value, "tps": tps}
        return value

    # ----- Williams %R ---------------------------------------------------

    def williams_r(self, bars: Sequence[Any], period: int = 14) -> Optional[float]:
        """Williams %R (−100–0) at ``bars[-1]``.

        Pre: ``period >= 1``. Returns ``None`` until ``len(bars) >= period``.
        Post: ``−100 × (highest_high − close) / (highest_high − lowest_low)``
        over the trailing ``period`` bars; ``−50.0`` (neutral) when the range is
        0, mirroring :meth:`stochastic`'s flat-window convention. Keeps a bounded
        ``(high, low)`` deque (like :meth:`donchian`) so the warm ``expand``/``slide``
        path appends one bar instead of re-slicing ``bars[-period:]`` each call; the
        extrema are still recomputed over the deque, so the value is identical.
        """
        if not bars or len(bars) < period:
            return None
        # If two symbols (or two unrelated bar streams) ever share a registry,
        # the cache must not conflate them — include ``bars[-1].symbol`` in the
        # key so the slots are disjoint, mirroring :meth:`macd`.
        symbol = _safe_getattr(bars[-1], "symbol")
        key = ("williams_r", symbol, period)
        fp = self._bar_fingerprint(bars)
        state = self._peek(key)
        if state is not None and self._is_same_bar(state, fp):
            return state["value"]
        hl: Optional[Deque[Tuple[float, float]]] = None
        if state is not None and "hl" in state:
            kind = self._advance_kind(state, bars, fp)
            if kind in ("expand", "slide"):
                hl = state["hl"]
                hl.append((float(bars[-1].high), float(bars[-1].low)))
        if hl is None:
            hl = deque(maxlen=period)
            for b in bars[-period:]:
                hl.append((float(b.high), float(b.low)))
        highest = max(t[0] for t in hl)
        lowest = min(t[1] for t in hl)
        rng = highest - lowest
        close = float(bars[-1].close)
        value = -50.0 if rng == 0 else -100.0 * (highest - close) / rng
        self._state[key] = {"fp": fp, "value": value, "hl": hl}
        return value
