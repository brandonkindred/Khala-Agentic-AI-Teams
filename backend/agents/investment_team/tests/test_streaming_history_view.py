"""Tests for the ``StreamingHistoryView`` cache behaviour.

The view's contract is **same-bar dedupe**: within one bar, multiple
predicates that reference the same ``IndicatorRef`` share the cached
``pd.Series``. Once a new bar is appended the cache invalidates and the
next access rebuilds the DataFrame and any cached indicator series.
Invalidation is driven by a monotonic per-instance append counter so
CPython id-reuse on the bounded deque cannot produce a false cache hit.
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


def _fresh_view_value(bars: list[BarRecord], ref: IndicatorRef, i: int) -> float | None:
    """Build a fresh view seeded with ``bars`` and read ``indicator(ref, i)``.

    Used as the bit-exact reference for cache-consistency checks: any
    stale-cache regression produces a value that differs from the
    fresh-view computation on the same input.
    """
    fresh = StreamingHistoryView(max_bars=max(len(bars), 1))
    for b in bars:
        fresh.append(b)
    return fresh.indicator(ref, i)


def test_same_bar_returns_cached_series() -> None:
    """Within one bar, repeat calls reuse the cached DataFrame + indicator
    series object — the whole point of the view."""
    view = StreamingHistoryView(max_bars=50)
    for i in range(20):
        view.append(_bar(i))
    ref = IndicatorRef(name="ema", params={"period": 5}, source="close")
    view.indicator(ref, 19)
    cached_series = view._indicator_cache[ref.sig_id]
    cached_df = view._df
    # No append → caches reused as-is.
    same = view.indicator(ref, 19)
    assert view._indicator_cache[ref.sig_id] is cached_series
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


def test_repeated_appends_match_fresh_view_value() -> None:
    """After every append, the cached indicator value must equal the
    value a freshly-constructed view (seeded with the same bar tail)
    returns. AND the append counter must advance one step per append
    (defends against an off-by-one in the counter bump that the
    monotone-close inequality check would otherwise miss)."""
    ref = IndicatorRef(name="sma", params={"period": 3}, source="close")
    view = StreamingHistoryView(max_bars=5)
    for i in range(5):
        view.append(_bar(i))
    assert view._append_counter == 5
    for i in range(5, 12):
        before = view._append_counter
        view.append(_bar(i))
        assert view._append_counter == before + 1, (
            f"counter bumped by {view._append_counter - before}, not 1"
        )
        live = view.indicator(ref, 4)
        # The view holds the trailing 5 bars (deque maxlen=5), so the
        # fresh-reference must be seeded with the same tail.
        expected = _fresh_view_value([_bar(j) for j in range(i - 4, i + 1)], ref, 4)
        assert live == expected, f"i={i} live={live!r} expected={expected!r}"


def test_batched_appends_do_not_yield_stale_cache_under_id_reuse() -> None:
    """The cache key is keyed on a monotonic append counter, not on
    ``id(self._bars[-1])`` — CPython recycles freed dataclass slots, so
    a sequence of appends without intervening ``indicator()`` calls
    could otherwise place a fresh BarRecord at an address recently
    freed by the evicted oldest bar and produce a false cache hit.

    Asserts BOTH the public observable (live value matches fresh view)
    AND the internal counter invariant: the cache_counter must lag the
    append_counter after batched appends, then catch up on the next
    indicator() call. Pins the counter mechanism directly so the test
    fails loudly under any revert to the id-based key, regardless of
    whether CPython id-reuse happens to fire on the run.
    """
    ref = IndicatorRef(name="sma", params={"period": 3}, source="close")
    view = StreamingHistoryView(max_bars=5)
    for i in range(5):
        view.append(_bar(i))
    # Seed the cache.
    view.indicator(ref, 4)
    assert view._cache_counter == view._append_counter == 5

    # Batched appends with NO intervening indicator() calls.
    for i in range(5, 20):
        view.append(_bar(i))
    # The cache_counter must lag the append_counter by exactly the
    # number of unflushed appends — pins counter-based invalidation
    # independent of CPython id-reuse semantics.
    assert view._append_counter == 20
    assert view._cache_counter == 5, (
        "cache_counter must NOT have advanced silently — the public "
        "API never accessed the cache during the batched appends."
    )

    # Now query indicator() — the counter mismatch must trigger a
    # rebuild against the live deque.
    live = view.indicator(ref, 4)
    expected = _fresh_view_value([_bar(j) for j in range(15, 20)], ref, 4)
    assert live == expected, f"batched-append stale cache: live={live!r} expected={expected!r}"
    # After indicator(), cache_counter catches up.
    assert view._cache_counter == view._append_counter == 20


def test_indicator_returns_none_on_empty_view() -> None:
    """``indicator()`` must defend its precondition: an empty view
    returns ``None`` instead of raising KeyError from pandas when the
    DataFrame has no OHLCV columns."""
    view = StreamingHistoryView()
    ref = IndicatorRef(name="sma", params={"period": 5}, source="close")
    assert view.indicator(ref, 0) is None


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
    assert sma_ref.sig_id in view._indicator_cache
    assert ema_ref.sig_id in view._indicator_cache
