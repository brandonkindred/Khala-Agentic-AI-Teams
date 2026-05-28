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
from typing import Any, Dict, Literal, Optional, Protocol, Sequence, Tuple

import pandas as pd

from ..executor import indicators as ind
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


def compute_indicator_series(ref: IndicatorRef, df: pd.DataFrame) -> pd.Series:
    """Compute the full indicator series for ``ref`` on ``df``.

    Pre: ``df`` has the standard OHLCV columns.
    Post: returns a ``pd.Series`` aligned with ``df``'s index.
    NaN during warmup is pandas' natural behaviour.
    """
    name = ref.name
    if name == "sma":
        return ind.sma(select_source_series(df, ref.source), int(ref.param("period")))
    if name == "ema":
        return ind.ema(select_source_series(df, ref.source), int(ref.param("period")))
    if name == "rsi":
        return ind.rsi(select_source_series(df, ref.source), int(ref.param("period")))
    if name == "macd":
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
    if name == "bollinger":
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
    if name == "atr":
        return ind.atr(df["high"], df["low"], df["close"], period=int(ref.param("period")))
    if name == "adx":
        return ind.adx(df["high"], df["low"], df["close"], period=int(ref.param("period")))
    if name == "stochastic":
        pct_k, pct_d = ind.stochastic(
            df["high"],
            df["low"],
            df["close"],
            k_period=int(ref.param("k_period")),
            d_period=int(ref.param("d_period")),
        )
        return pct_d if ref.param("output") == "d" else pct_k
    if name == "vwap":
        return ind.vwap(df["high"], df["low"], df["close"], df["volume"])
    raise ValueError(f"unknown indicator name: {name!r}")


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

    def length(self) -> int:
        return len(self._df)

    def bar_field(self, field_name: str, i: int) -> float:
        return float(self._df[field_name].iloc[i])

    def indicator(self, ref: IndicatorRef, i: int) -> Optional[float]:
        key = ref.model_dump_json()
        if key not in self._cache:
            self._cache[key] = compute_indicator_series(ref, self._df)
        series = self._cache[key]
        if i >= len(series):
            return None
        value = series.iloc[i]
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


class StreamingHistoryView:
    """``HistoryView`` backed by a bounded deque of bars.

    Designed for the engine's per-bar loop. Bars are appended
    incrementally; indicator values are computed against the full deque
    on demand and cached so repeated predicates within the same bar
    share the work.

    Earlier revisions of this view dropped the DataFrame and cleared the
    indicator cache on every ``append``, which forced a full O(N) pandas
    rebuild for the next predicate evaluation. The view now keeps the
    DataFrame and the indicator series alive across bars: when only a
    single new bar has been appended since the last computation, the
    indicator series is extended by one row instead of recomputed.

    The deque is bounded to ``max_bars`` (default 500, matching the
    ``StrategyContext._ingest_bar`` retention ceiling). When the bounded
    deque rolls over (oldest row dropped on append), the cache falls back
    to a full rebuild on next access — that path runs at most once per
    ring rollover, so the amortised cost remains ``O(1)`` per bar.
    """

    def __init__(self, max_bars: int = 500) -> None:
        self._bars: deque[BarRecord] = deque(maxlen=max_bars)
        self._df: Optional[pd.DataFrame] = None
        # Number of rows the cached ``_df`` and each cached indicator
        # series cover. Drifts behind ``len(self._bars)`` until the next
        # ``_sync`` runs from ``indicator()`` / ``_ensure_df()``.
        self._df_rows: int = 0
        self._indicator_cache: Dict[str, pd.Series] = {}
        # True when the next ``_sync`` cannot be incremental — typically
        # because the bounded deque rolled over and the cached prefix is
        # no longer aligned with the live deque.
        self._needs_full_rebuild: bool = False

    def append(self, bar: BarRecord) -> None:
        """Append a bar, marking caches for incremental refresh.

        The DataFrame / indicator cache is NOT cleared here. The next
        ``indicator()`` or ``_ensure_df()`` call observes
        ``len(self._bars) != self._df_rows`` and either appends one row
        (the common case) or rebuilds from scratch when the bounded
        deque has rolled over.
        """
        rollover = len(self._bars) == self._bars.maxlen
        self._bars.append(bar)
        if rollover:
            # Oldest row was just dropped — cached prefix is stale.
            self._needs_full_rebuild = True

    def length(self) -> int:
        return len(self._bars)

    def bar_field(self, field_name: str, i: int) -> float:
        b = self._bars[i]
        return float(getattr(b, field_name))

    def indicator(self, ref: IndicatorRef, i: int) -> Optional[float]:
        df = self._ensure_df()
        key = ref.model_dump_json()
        series = self._indicator_cache.get(key)
        if series is None or len(series) != len(df):
            # Either first call for this ref or the deque rolled over and
            # forced a rebuild — recompute against the live DataFrame.
            series = compute_indicator_series(ref, df)
            self._indicator_cache[key] = series
        if i >= len(series):
            return None
        value = series.iloc[i]
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return None
        return float(value)

    def _ensure_df(self) -> pd.DataFrame:
        if (
            self._df is not None
            and not self._needs_full_rebuild
            and self._df_rows == len(self._bars)
        ):
            return self._df

        if (
            self._df is not None
            and not self._needs_full_rebuild
            and self._df_rows < len(self._bars)
        ):
            # Incremental extend: append only the new rows.
            tail = list(self._bars)[self._df_rows :]
            new_rows = pd.DataFrame(
                [
                    {
                        "open": b.open,
                        "high": b.high,
                        "low": b.low,
                        "close": b.close,
                        "volume": b.volume,
                    }
                    for b in tail
                ]
            )
            self._df = pd.concat([self._df, new_rows], ignore_index=True)
            self._df_rows = len(self._bars)
            return self._df

        # Cold start or rollover-triggered rebuild.
        rows = [
            {
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.volume,
            }
            for b in self._bars
        ]
        self._df = pd.DataFrame(rows)
        self._df_rows = len(self._bars)
        self._needs_full_rebuild = False
        # The DataFrame's row count just changed shape — any previously
        # cached indicator series is misaligned and must be rebuilt on
        # next access. ``indicator()`` already gates on
        # ``len(series) != len(df)``, so we just drop the cache here.
        self._indicator_cache.clear()
        return self._df
