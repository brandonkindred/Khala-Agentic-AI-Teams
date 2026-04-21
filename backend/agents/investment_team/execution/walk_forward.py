"""Purged, embargoed walk-forward fold construction.

Splits a single backtest date span into ``k_folds`` contiguous test blocks and
derives each fold's training segments as the complement — with a López de
Prado-style purge cushion adjacent to the test window and an embargo window
immediately after. Purge keeps training trades whose label horizon would
overlap the test out of training; embargo keeps post-test serial correlation
from leaking back into subsequent training.

This module is pure data construction — it does not execute strategies or
consume market data. Step 8 of issue #247 wires it into the Strategy Lab
orchestrator for terminal acceptance-gate evaluation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING, List, Sequence, Tuple, Union

if TYPE_CHECKING:  # pragma: no cover
    from ..models import TradeRecord


DateLike = Union[str, date]


def _parse_date(d: DateLike) -> date:
    if isinstance(d, date):
        return d
    return date.fromisoformat(d[:10])


def _weekday_range(start: date, end: date) -> List[date]:
    out: List[date] = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


@dataclass(frozen=True)
class DateRange:
    start: date
    end: date

    def contains(self, d: date) -> bool:
        return self.start <= d <= self.end


@dataclass(frozen=True)
class Fold:
    """One walk-forward fold: a test block plus its complement training set.

    ``train_ranges`` is a tuple so a fold is hashable; purging may leave the
    training set with up to two disjoint segments (pre-test and post-test), or
    zero segments when K=1.
    """

    fold_index: int
    train_ranges: Tuple[DateRange, ...]
    test_range: DateRange

    @property
    def test_start(self) -> date:
        return self.test_range.start

    @property
    def test_end(self) -> date:
        return self.test_range.end


def build_purged_walk_forward(
    start: DateLike,
    end: DateLike,
    *,
    k_folds: int = 5,
    embargo_days: int = 0,
    purge_hold_days: int = 0,
) -> List[Fold]:
    """Partition ``[start, end]`` into K contiguous test blocks with purge + embargo.

    Each fold's test block is one of K near-equal-sized weekday windows tiling
    the span with no gaps. Training segments are the complement, shrunk by
    ``purge_hold_days`` calendar days on the test-adjacent side (to exclude
    trades whose label horizon straddles the test) and by ``embargo_days`` on
    the post-test side (to exclude serially-correlated training trades that
    start just after the test ends).

    Raises ``ValueError`` when the span is empty, too short for K folds, or
    parameters are negative.
    """
    if k_folds < 1:
        raise ValueError(f"k_folds must be >= 1, got {k_folds}")
    if embargo_days < 0:
        raise ValueError(f"embargo_days must be non-negative, got {embargo_days}")
    if purge_hold_days < 0:
        raise ValueError(f"purge_hold_days must be non-negative, got {purge_hold_days}")

    span_start = _parse_date(start)
    span_end = _parse_date(end)
    if span_end < span_start:
        raise ValueError(f"end ({span_end}) precedes start ({span_start})")

    weekdays = _weekday_range(span_start, span_end)
    if len(weekdays) < k_folds:
        raise ValueError(
            f"span has {len(weekdays)} weekday(s), need at least {k_folds} for k_folds={k_folds}"
        )

    folds: List[Fold] = []
    n = len(weekdays)
    for i in range(k_folds):
        lo = (n * i) // k_folds
        hi = (n * (i + 1)) // k_folds
        test_start = weekdays[lo]
        test_end = weekdays[hi - 1]

        train_segments: List[DateRange] = []
        pre_end = test_start - timedelta(days=purge_hold_days + 1)
        if pre_end >= span_start:
            train_segments.append(DateRange(span_start, pre_end))
        post_start = test_end + timedelta(days=embargo_days + 1)
        if post_start <= span_end:
            train_segments.append(DateRange(post_start, span_end))

        folds.append(
            Fold(
                fold_index=i,
                train_ranges=tuple(train_segments),
                test_range=DateRange(test_start, test_end),
            )
        )
    return folds


def filter_trades_in_range(
    trades: Sequence["TradeRecord"],
    start: DateLike,
    end: DateLike,
) -> List["TradeRecord"]:
    """Return trades whose ``exit_date`` falls in ``[start, end]``.

    Exit-based bucketing assigns each trade to exactly one window (the one
    where its outcome was realized), which is the semantic the acceptance gate
    needs for OOS trade counts and OOS equity construction.
    """
    s = _parse_date(start)
    e = _parse_date(end)
    out: List["TradeRecord"] = []
    for t in trades:
        exit_d = _parse_date(t.exit_date)
        if s <= exit_d <= e:
            out.append(t)
    return out


def filter_trades_in_fold_training(
    trades: Sequence["TradeRecord"],
    fold: Fold,
) -> List["TradeRecord"]:
    """Return trades whose ``exit_date`` falls within any of the fold's training segments."""
    out: List["TradeRecord"] = []
    for t in trades:
        exit_d = _parse_date(t.exit_date)
        if any(r.contains(exit_d) for r in fold.train_ranges):
            out.append(t)
    return out


def max_hold_days_from_trades(trades: Sequence["TradeRecord"]) -> int:
    """Largest ``hold_days`` across the trade set, or 0 when empty."""
    if not trades:
        return 0
    return max(t.hold_days for t in trades)
