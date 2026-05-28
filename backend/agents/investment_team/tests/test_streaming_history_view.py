"""Tests for the ``StreamingHistoryView`` cache behaviour.

The view's contract is **same-bar dedupe**: within one bar, multiple
predicates that reference the same ``IndicatorRef`` share the cached
``pd.Series``. Once a new bar is appended the cache invalidates and the
next access rebuilds. These tests pin that contract and verify the
rollover path correctly invalidates the cache when the bounded deque
drops its oldest bar.
"""

from __future__ import annotations

from investment_team.strategy_lab.executor.predicate_evaluator import (
    BarRecord,
    StreamingHistoryView,
)
from investment_team.strategy_lab.spec_dsl import IndicatorRef


def _bar(i: int) -> BarRecord:
    return BarRecord(
        timestamp=f"2024-01-{i + 1:02d}",
        open=100.0,
        high=101.0 + i * 0.1,
        low=99.0 - i * 0.1,
        close=100.0 + i * 0.2,
        volume=1000.0 + i,
    )


def test_same_bar_returns_cached_series() -> None:
    """Within one bar, repeat calls reuse the cached DataFrame + indicator
    series object — the whole point of the view."""
    view = StreamingHistoryView(max_bars=50)
    for i in range(20):
        view.append(_bar(i))
    ref = IndicatorRef(name="ema", params={"period": 5}, source="close")
    view.indicator(ref, 19)
    cached_series = view._indicator_cache[ref.model_dump_json()]
    cached_df = view._df
    # No append → caches reused as-is.
    same = view.indicator(ref, 19)
    assert view._indicator_cache[ref.model_dump_json()] is cached_series
    assert view._df is cached_df
    assert same is not None


def test_append_invalidates_cache_on_next_access() -> None:
    """An append marks the cache stale; the next ``indicator()`` call
    rebuilds and returns a value reflecting the new bar."""
    view = StreamingHistoryView(max_bars=50)
    for i in range(20):
        view.append(_bar(i))
    ref = IndicatorRef(name="sma", params={"period": 5}, source="close")
    v1 = view.indicator(ref, 19)
    assert v1 is not None
    view.append(_bar(20))
    v2 = view.indicator(ref, 20)
    assert v2 is not None
    assert len(view._df) == 21
    assert v2 != v1  # SMA window slid forward by one bar


def test_rollover_rebuilds_cache_against_live_deque() -> None:
    """Appending past max_bars drops the oldest bar; the next access must
    return values aligned with the new (rotated) deque, not the stale
    pre-rollover prefix."""
    view = StreamingHistoryView(max_bars=10)
    for i in range(10):
        view.append(_bar(i))
    ref = IndicatorRef(name="sma", params={"period": 3}, source="close")
    pre = view.indicator(ref, 9)
    assert pre is not None
    # Rollover: deque pops bar 0, appends bar 10.
    view.append(_bar(10))
    val = view.indicator(ref, 9)
    assert val is not None
    assert len(view._df) == 10
    # The cache was rebuilt against the new deque tail: SMA over bars
    # [8, 9, 10] differs from the pre-rollover SMA over [7, 8, 9].
    assert val != pre


def test_repeated_appends_do_not_accumulate_stale_cache() -> None:
    """Successive appends without a rebuild in between must not silently
    return values from an earlier bar. Regression check for the prior
    revision's ``_needs_full_rebuild=True`` set on every saturated
    append: that flag bypassed the cache reuse entirely, masking the
    bug below."""
    view = StreamingHistoryView(max_bars=5)
    for i in range(5):
        view.append(_bar(i))
    ref = IndicatorRef(name="sma", params={"period": 3}, source="close")
    last = view.indicator(ref, 4)
    for i in range(5, 12):
        view.append(_bar(i))
        # Each indicator() call must see the freshly-appended bar.
        new_val = view.indicator(ref, 4)
        assert new_val is not None
        assert new_val != last
        last = new_val


def test_multi_indicator_share_dataframe() -> None:
    view = StreamingHistoryView(max_bars=30)
    for i in range(20):
        view.append(_bar(i))
    sma_ref = IndicatorRef(name="sma", params={"period": 5}, source="close")
    ema_ref = IndicatorRef(name="ema", params={"period": 5}, source="close")
    view.indicator(sma_ref, 19)
    view.indicator(ema_ref, 19)
    # Both indicators computed against the same shared DataFrame.
    assert len(view._df) == 20
    assert sma_ref.model_dump_json() in view._indicator_cache
    assert ema_ref.model_dump_json() in view._indicator_cache
