"""Shared predicate evaluator for engine runtime and alignment audit.

Promotes the deterministic predicate-evaluation logic from the alignment
gate (``alignment_checks.py``) into a reusable module behind a
``HistoryView`` protocol.  Both the engine's per-bar entry/exit
dispatchers and the post-hoc alignment audit consume these functions,
guaranteeing identical evaluation semantics.

The module is intentionally side-effect-free: every function takes an
immutable view of market data and returns a pure result.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Dict, Literal, Optional, Protocol, Sequence, Tuple

import pandas as pd

from ..executor import indicators as ind
from ..indicators.streaming import IndicatorRegistry
from ..spec_dsl import (
    EntryRule,
    IndicatorRef,
    Predicate,
    SignalExitRule,
)

# ---------------------------------------------------------------------------
# HistoryView protocol
# ---------------------------------------------------------------------------

_PRICE_REF_FIELDS: dict[str, str] = {
    "bar.close": "close",
    "bar.high": "high",
    "bar.low": "low",
    "bar.volume": "volume",
}


class HistoryView(Protocol):
    """Read-only view over a symbol's bar history + indicator values."""

    def length(self) -> int: ...
    def bar_field(self, field_name: str, i: int) -> float: ...
    def indicator(self, ref: IndicatorRef, i: int) -> Optional[float]: ...


# ---------------------------------------------------------------------------
# EvaluationResult
# ---------------------------------------------------------------------------

EvalStatus = Literal["satisfied", "miss", "warmup"]


@dataclass(frozen=True)
class EvaluationResult:
    status: EvalStatus
    lhs: Optional[float] = None
    rhs: Optional[float] = None
    rel_miss: Optional[float] = None


# ---------------------------------------------------------------------------
# Pure evaluation functions
# ---------------------------------------------------------------------------


def resolve_side_value(
    side: Any,
    view: HistoryView,
    i: int,
) -> Optional[float]:
    """Resolve one side of a predicate to a scalar at bar index ``i``.

    Pre: ``i`` is in ``[0, view.length())``.
    Post: returns ``None`` when the indicator value is NaN (warmup);
    ``float`` literals and bar-field references are always resolvable.
    """
    if isinstance(side, IndicatorRef):
        return view.indicator(side, i)
    if isinstance(side, str):
        col = _PRICE_REF_FIELDS.get(side)
        if col is None:
            raise ValueError(f"unexpected bar-ref string: {side!r}")
        return view.bar_field(col, i)
    if isinstance(side, (int, float)) and not isinstance(side, bool):
        return float(side)
    raise TypeError(f"unsupported predicate side type: {type(side).__name__}")


def compare(
    op: str,
    lhs: float,
    rhs: float,
    *,
    prev_lhs: Optional[float] = None,
    prev_rhs: Optional[float] = None,
) -> bool:
    """Evaluate a comparison op on two scalars.

    ``cross_above`` / ``cross_below`` require previous-bar values;
    returns ``False`` (fail-closed) when they are unavailable.
    """
    if op == "<":
        return lhs < rhs
    if op == "<=":
        return lhs <= rhs
    if op == ">":
        return lhs > rhs
    if op == ">=":
        return lhs >= rhs
    if op == "==":
        return math.isclose(lhs, rhs, rel_tol=1e-9, abs_tol=1e-12)
    if op == "cross_above":
        if prev_lhs is None or prev_rhs is None:
            return False
        return prev_lhs <= prev_rhs and lhs > rhs
    if op == "cross_below":
        if prev_lhs is None or prev_rhs is None:
            return False
        return prev_lhs >= prev_rhs and lhs < rhs
    raise ValueError(f"unknown comparison op: {op!r}")


def relative_miss(computed: float, threshold: float) -> float:
    """``|computed - threshold| / max(|threshold|, |computed|, 1e-12)``."""
    denom = max(abs(threshold), abs(computed), 1e-12)
    return abs(computed - threshold) / denom


def evaluate_predicate(
    pred: Predicate,
    view: HistoryView,
    i: int,
) -> EvaluationResult:
    """Evaluate a single predicate at bar index ``i``.

    Pre: ``i`` is in ``[0, view.length())``.
    Post: ``status`` is ``"satisfied"`` when the predicate is true,
    ``"miss"`` when it is false, or ``"warmup"`` when an indicator
    returns ``None`` (insufficient history).
    """
    try:
        lhs_val = resolve_side_value(pred.lhs, view, i)
        rhs_val = resolve_side_value(pred.rhs, view, i)
    except (ValueError, TypeError):
        return EvaluationResult(status="warmup")

    if lhs_val is None or rhs_val is None:
        return EvaluationResult(status="warmup")

    prev_lhs: Optional[float] = None
    prev_rhs: Optional[float] = None
    if pred.op in ("cross_above", "cross_below") and i > 0:
        try:
            prev_lhs = resolve_side_value(pred.lhs, view, i - 1)
            prev_rhs = resolve_side_value(pred.rhs, view, i - 1)
        except (ValueError, TypeError):
            pass

    is_cross = pred.op in ("cross_above", "cross_below")
    if is_cross and (prev_lhs is None or prev_rhs is None) and i == 0:
        return EvaluationResult(status="warmup", lhs=lhs_val, rhs=rhs_val)

    satisfied = compare(
        pred.op,
        lhs_val,
        rhs_val,
        prev_lhs=prev_lhs,
        prev_rhs=prev_rhs,
    )
    if satisfied:
        return EvaluationResult(status="satisfied", lhs=lhs_val, rhs=rhs_val, rel_miss=0.0)

    rm = None if is_cross else relative_miss(lhs_val, rhs_val)
    return EvaluationResult(status="miss", lhs=lhs_val, rhs=rhs_val, rel_miss=rm)


def evaluate_entry_rules(
    rules: Sequence[EntryRule],
    view: HistoryView,
    i: int,
    *,
    side_filter: Optional[str] = None,
) -> Optional[Tuple[EntryRule, int]]:
    """Return the first entry rule whose predicate fires at bar ``i``.

    Pre: ``rules`` is the spec's ``entry_rules`` list.
    Post: returns ``(rule, original_index)`` or ``None``.
    """
    for idx, rule in enumerate(rules):
        if not isinstance(rule, EntryRule):
            continue
        if side_filter is not None and rule.side != side_filter:
            continue
        result = evaluate_predicate(rule.when, view, i)
        if result.status == "satisfied":
            return rule, idx
    return None


def evaluate_signal_exit_rules(
    rules: Sequence[Any],
    view: HistoryView,
    i: int,
) -> Optional[Tuple[SignalExitRule, int]]:
    """Return the first signal-exit rule whose predicate fires at bar ``i``.

    Pre: ``rules`` is the spec's ``exit_rules`` list (may contain
    non-``SignalExitRule`` members, which are skipped).
    Post: returns ``(rule, original_index)`` or ``None``.
    """
    for idx, rule in enumerate(rules):
        if not isinstance(rule, SignalExitRule):
            continue
        result = evaluate_predicate(rule.when, view, i)
        if result.status == "satisfied":
            return rule, idx
    return None


# ---------------------------------------------------------------------------
# Indicator computation (shared by both HistoryView implementations)
# ---------------------------------------------------------------------------


def select_source_series(df: pd.DataFrame, source: str) -> pd.Series:
    """Return the input series the indicator should read from."""
    if source == "hl2":
        return (df["high"] + df["low"]) / 2.0
    if source == "ohlc4":
        return (df["open"] + df["high"] + df["low"] + df["close"]) / 4.0
    return df[source]


def _series_sma(ref: IndicatorRef, df: pd.DataFrame) -> pd.Series:
    return ind.sma(select_source_series(df, ref.source), int(ref.param("period")))


def _series_ema(ref: IndicatorRef, df: pd.DataFrame) -> pd.Series:
    return ind.ema(select_source_series(df, ref.source), int(ref.param("period")))


def _series_rsi(ref: IndicatorRef, df: pd.DataFrame) -> pd.Series:
    return ind.rsi(select_source_series(df, ref.source), int(ref.param("period")))


def _series_macd(ref: IndicatorRef, df: pd.DataFrame) -> pd.Series:
    series = select_source_series(df, ref.source)
    macd_line, signal_line, hist = ind.macd(
        series,
        fast=int(ref.param("fast")),
        slow=int(ref.param("slow")),
        signal=int(ref.param("signal")),
    )
    output = ref.param("output")
    if output == "signal":
        return signal_line
    if output == "histogram":
        return hist
    return macd_line


def _series_bollinger(ref: IndicatorRef, df: pd.DataFrame) -> pd.Series:
    series = select_source_series(df, ref.source)
    upper, middle, lower = ind.bollinger_bands(
        series,
        period=int(ref.param("period")),
        num_std=float(ref.param("num_std")),
    )
    band = ref.param("band")
    if band == "upper":
        return upper
    if band == "lower":
        return lower
    return middle


def _series_atr(ref: IndicatorRef, df: pd.DataFrame) -> pd.Series:
    return ind.atr(df["high"], df["low"], df["close"], period=int(ref.param("period")))


def _series_adx(ref: IndicatorRef, df: pd.DataFrame) -> pd.Series:
    return ind.adx(df["high"], df["low"], df["close"], period=int(ref.param("period")))


def _series_stochastic(ref: IndicatorRef, df: pd.DataFrame) -> pd.Series:
    pct_k, pct_d = ind.stochastic(
        df["high"],
        df["low"],
        df["close"],
        k_period=int(ref.param("k_period")),
        d_period=int(ref.param("d_period")),
    )
    return pct_d if ref.param("output") == "d" else pct_k


def _series_vwap(ref: IndicatorRef, df: pd.DataFrame) -> pd.Series:
    return ind.vwap(df["high"], df["low"], df["close"], df["volume"])


# Module-level O(1) dispatch table — one entry per DSL ``IndicatorName``.
# Replaces a linear if/elif chain so indicator lookup is constant-time on
# the predicate-evaluation hot path.
_INDICATOR_SERIES_DISPATCH: Dict[str, Callable[[IndicatorRef, pd.DataFrame], pd.Series]] = {
    "sma": _series_sma,
    "ema": _series_ema,
    "rsi": _series_rsi,
    "macd": _series_macd,
    "bollinger": _series_bollinger,
    "atr": _series_atr,
    "adx": _series_adx,
    "stochastic": _series_stochastic,
    "vwap": _series_vwap,
}


def compute_indicator_series(ref: IndicatorRef, df: pd.DataFrame) -> pd.Series:
    """Compute the full indicator series for ``ref`` on ``df``.

    Pre: ``df`` has the standard OHLCV columns; ``ref.name`` is a known
    DSL indicator name.
    Post: returns a ``pd.Series`` aligned with ``df``'s index.
    NaN during warmup is pandas' natural behaviour.
    """
    builder = _INDICATOR_SERIES_DISPATCH.get(ref.name)
    if builder is None:
        raise ValueError(f"unknown indicator name: {ref.name!r}")
    return builder(ref, df)


# ---------------------------------------------------------------------------
# PandasHistoryView — wraps the alignment gate's (DataFrame, cache) pair
# ---------------------------------------------------------------------------


class PandasHistoryView:
    """``HistoryView`` backed by a pre-built DataFrame + indicator cache.

    Used by the alignment gate to evaluate predicates against full
    market-data frames. The indicator cache is populated lazily and
    shared across trades for the same symbol.
    """

    def __init__(self, df: pd.DataFrame, indicator_cache: Dict[str, pd.Series]) -> None:
        self._df = df
        self._cache = indicator_cache
        # Private per-view ``ndarray`` views for O(1) scalar reads on the
        # predicate hot path. pandas ``.iloc[i]`` scalar indexing carries heavy
        # per-call overhead (label resolution, scalar boxing); indexing a cached
        # numpy array does not, and the values are bit-identical. The shared
        # ``indicator_cache`` (``Dict[str, pd.Series]``) contract is unchanged —
        # these arrays are derived from it lazily and never replace it.
        self._col_arrays: Dict[str, Any] = {}
        self._series_arrays: Dict[str, Any] = {}

    def length(self) -> int:
        return len(self._df)

    def bar_field(self, field_name: str, i: int) -> float:
        arr = self._col_arrays.get(field_name)
        if arr is None:
            arr = self._df[field_name].to_numpy()
            self._col_arrays[field_name] = arr
        return float(arr[i])

    def indicator(self, ref: IndicatorRef, i: int) -> Optional[float]:
        key = ref.sig_id
        arr = self._series_arrays.get(key)
        if arr is None:
            series = self._cache.get(key)
            if series is None:
                series = compute_indicator_series(ref, self._df)
                self._cache[key] = series
            arr = series.to_numpy()
            self._series_arrays[key] = arr
        if i >= len(arr):
            return None
        value = arr[i]
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return None
        return float(value)


# ---------------------------------------------------------------------------
# StreamingHistoryView — deque-backed, for the engine's per-bar runtime
# ---------------------------------------------------------------------------


@dataclass
class BarRecord:
    """Minimal bar representation for the streaming view."""

    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    # Per-symbol views carry ``symbol=None`` (a single bar stream), which the
    # registry's MACD symbol-slotted cache key handles as one slot. Populated
    # by the engine so the registry's multi-stream precondition holds explicitly
    # if a view is ever shared across symbols.
    symbol: Optional[str] = None


# Map each DSL ``IndicatorName`` to the registry method + the params it reads
# from the ``IndicatorRef``. Mirrors ``_INDICATOR_SERIES_DISPATCH`` but routes
# the engine's per-bar reads through the streaming ``IndicatorRegistry`` (O(1)
# amortised recurrences) instead of a full pandas ``pd.Series`` recompute.
def _registry_indicator(
    reg: IndicatorRegistry, ref: IndicatorRef, bars: Sequence[Any]
) -> Optional[float]:
    """Trailing-bar value of ``ref`` over ``bars`` via the streaming registry.

    Pre: ``ref.name`` is a known DSL indicator; ``ref.params`` has its
    defaults filled (guaranteed by ``IndicatorRef`` validation). ``bars`` is a
    list-like the registry can slice/index (``bars[-period:]``, ``bars[i]``).
    Post: the indicator's scalar value at ``bars[-1]``, or ``None`` during
    warm-up — byte-identical to a fresh ``IndicatorRegistry`` over the same
    ``bars`` (so engine and sandbox ``ctx.indicator`` agree).
    """
    name = ref.name
    if name == "sma":
        return reg.sma(bars, period=int(ref.param("period")), source=ref.source)
    if name == "ema":
        return reg.ema(bars, period=int(ref.param("period")), source=ref.source)
    if name == "rsi":
        return reg.rsi(bars, period=int(ref.param("period")), source=ref.source)
    if name == "macd":
        return reg.macd(
            bars,
            fast=int(ref.param("fast")),
            slow=int(ref.param("slow")),
            signal=int(ref.param("signal")),
            source=ref.source,
            select=str(ref.param("output")),
        )
    if name == "bollinger":
        return reg.bollinger_bands(
            bars,
            period=int(ref.param("period")),
            num_std=float(ref.param("num_std")),
            source=ref.source,
            select=str(ref.param("band")),
        )
    if name == "atr":
        return reg.atr(bars, period=int(ref.param("period")))
    if name == "adx":
        return reg.adx(bars, period=int(ref.param("period")))
    if name == "stochastic":
        return reg.stochastic(
            bars,
            k_period=int(ref.param("k_period")),
            d_period=int(ref.param("d_period")),
            select=str(ref.param("output")),
        )
    if name == "vwap":
        return reg.vwap(bars)
    raise ValueError(f"unknown indicator name: {name!r}")


class StreamingHistoryView:
    """``HistoryView`` backed by a bounded deque of bars + a streaming registry.

    Designed for the engine's per-bar loop. Each appended bar advances a
    retained :class:`IndicatorRegistry` by a single recurrence step, and the
    resulting scalar is appended to a per-``ref.sig_id`` buffer aligned 1:1 with
    the bounded bars deque. Indexed reads — ``indicator(ref, i)`` is called with
    an explicit bar index ``i``, and ``cross_above`` / ``cross_below`` read both
    ``i`` and ``i - 1`` — are served straight from that buffer, so no indicator
    is ever recomputed over the full window.

    This replaces the previous design, which rebuilt the entire pandas
    DataFrame from the deque and recomputed every indicator's full ``pd.Series``
    (``rolling`` / ``ewm``) on every appended bar — ``O(window × num_indicators)``
    pandas work per bar. The registry recurrences are O(1) amortised (MACD) or
    O(window) (the windowed indicators), independent of how many bars have
    streamed through.

    Invariants:
    * ``len(self._scalar_buffers[sig_id]) == len(self._bars)`` for every
      registered ref once synced — buffer index ``i`` holds the indicator value
      at ``self._bars[i]``. Both are ``deque(maxlen=max_bars)`` and one value is
      pushed per appended bar, so they roll over in lockstep.
    * Warm-up returns ``None`` (the registry returns ``None`` until it has
      enough history), preserving the previous NaN→``None`` boundary semantics.

    Cache identity is driven by a monotonic per-instance ``_append_counter``
    bumped on every :meth:`append`. It anchors both the lazy ``list(deque)``
    snapshot (rebuilt once per bar, shared across all refs queried that bar) and
    each buffer's ``synced`` watermark, so a ref first queried mid-stream
    backfills correctly and a sparsely-queried ref catches up without a stale
    read. The counter is never recycled within a process.

    The deque is bounded to ``max_bars`` (default 500, matching the
    ``StrategyContext._ingest_bar`` retention ceiling — engine and sandbox must
    compute MACD/VWAP over the same trailing window for the conformance gate).
    """

    def __init__(self, max_bars: int = 500) -> None:
        self._bars: deque[BarRecord] = deque(maxlen=max_bars)
        self._max_bars = max_bars
        self._append_counter: int = 0
        # Lazy ``list(self._bars)`` snapshot — the registry needs a sliceable,
        # randomly-indexable sequence (a deque supports neither). Rebuilt once
        # per bar (keyed by the counter) and shared across every ref query on
        # that bar; bounded by ``max_bars`` so this is O(max_bars), not O(bars
        # seen), and carries none of pandas' per-call overhead.
        self._bars_list: list[BarRecord] = []
        self._bars_list_counter: Optional[int] = None
        # One registry for the view's lifetime; per-``sig_id`` scalar buffers.
        self._registry = IndicatorRegistry()
        # sig_id -> {"buf": deque[Optional[float]], "synced": int}
        self._buffers: Dict[str, Dict[str, Any]] = {}

    def append(self, bar: BarRecord) -> None:
        """Append a bar; buffers advance lazily on the next :meth:`indicator`."""
        self._bars.append(bar)
        self._append_counter += 1

    def length(self) -> int:
        return len(self._bars)

    def bar_field(self, field_name: str, i: int) -> float:
        b = self._bars[i]
        return float(getattr(b, field_name))

    def indicator(self, ref: IndicatorRef, i: int) -> Optional[float]:
        if not self._bars:
            return None
        bars_list = self._ensure_bars_list()
        st = self._buffers.get(ref.sig_id)
        if st is None:
            st = {"buf": deque(maxlen=self._max_bars), "synced": 0}
            self._buffers[ref.sig_id] = st
        self._sync_buffer(ref, st, bars_list)
        buf = st["buf"]
        if i < 0 or i >= len(buf):
            return None
        return buf[i]

    def _ensure_bars_list(self) -> list[BarRecord]:
        """Return ``list(self._bars)``, cached until the next append."""
        if self._bars_list_counter == self._append_counter:
            return self._bars_list
        self._bars_list = list(self._bars)
        self._bars_list_counter = self._append_counter
        return self._bars_list

    def _sync_buffer(
        self, ref: IndicatorRef, st: Dict[str, Any], bars_list: list[BarRecord]
    ) -> None:
        """Advance ``st['buf']`` so it holds one value per current bar.

        Pre: ``bars_list`` is the current ``list(self._bars)`` snapshot.
        Post: ``len(st['buf']) == len(bars_list)`` and ``buf[i]`` is the
        indicator value at ``bars_list[i]``; ``st['synced'] == _append_counter``.
        """
        ac = self._append_counter
        synced = st["synced"]
        if synced == ac:
            return  # already current (same-bar repeat query)
        buf = st["buf"]
        length = len(bars_list)
        base = ac - length  # absolute append-index of bars_list[0]
        reg = self._registry
        if synced < base:
            # The buffer fell behind by more than the window — the bars between
            # ``synced`` and ``base`` were evicted and can no longer be computed
            # or addressed. Rebuild over the currently-addressable deque by
            # forward-walking the registry (cold-start, then single-step
            # expand), which the registry detects from the bar fingerprints.
            buf.clear()
            for k in range(1, length + 1):
                prefix = bars_list if k == length else bars_list[:k]
                buf.append(_registry_indicator(reg, ref, prefix))
        else:
            # Contiguous catch-up: feed the bars appended since ``synced`` one at
            # a time so the registry sees each expand/slide transition (O(1)/bar
            # for MACD, O(window) for the windowed indicators). The bounded buf
            # evicts in lockstep with the bounded deque.
            for abs_idx in range(synced, ac):
                k = abs_idx - base + 1
                prefix = bars_list if k == length else bars_list[:k]
                buf.append(_registry_indicator(reg, ref, prefix))
        st["synced"] = ac
