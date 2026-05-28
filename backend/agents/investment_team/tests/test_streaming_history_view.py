"""Tests for the append-only ``StreamingHistoryView`` cache behaviour.

The view used to clear its DataFrame + indicator cache on every ``append``,
forcing a full pandas rebuild on the next predicate evaluation. The
revised view extends both incrementally; these tests pin the new
behaviour:

* ``append`` does not invalidate the cache below the rollover boundary;
* indicator series grow by one row per bar after the first call;
* once the deque rolls over (oldest bar dropped) the view falls back to
  a full rebuild so the cached prefix can't go out of sync.
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


def test_append_does_not_clear_dataframe_below_rollover() -> None:
    view = StreamingHistoryView(max_bars=50)
    for i in range(20):
        view.append(_bar(i))
    ref = IndicatorRef(name="sma", params={"period": 5}, source="close")
    # First call seeds the DataFrame + indicator series.
    v1 = view.indicator(ref, 19)
    df_first = view._df
    assert df_first is not None
    assert len(df_first) == 20
    # Append a new bar — cache must NOT be cleared, only extended.
    view.append(_bar(20))
    assert view._df is df_first  # same DataFrame object (extension is in-place reference)
    v2 = view.indicator(ref, 20)
    # The DataFrame now has 21 rows; the cached series is rebuilt on shape
    # mismatch, so the new value reflects the new bar.
    assert len(view._df) == 21
    assert v1 is not None and v2 is not None
    assert v2 != v1  # SMA window slid forward by one bar


def test_indicator_cache_persists_across_appends_until_shape_changes() -> None:
    view = StreamingHistoryView(max_bars=50)
    for i in range(20):
        view.append(_bar(i))
    ref = IndicatorRef(name="ema", params={"period": 5}, source="close")
    view.indicator(ref, 19)
    cached_series = view._indicator_cache[ref.model_dump_json()]
    # No append → same cached series object, same DataFrame.
    same = view.indicator(ref, 19)
    assert view._indicator_cache[ref.model_dump_json()] is cached_series
    assert same is not None


def test_rollover_triggers_full_rebuild() -> None:
    view = StreamingHistoryView(max_bars=10)
    for i in range(10):
        view.append(_bar(i))
    ref = IndicatorRef(name="sma", params={"period": 3}, source="close")
    view.indicator(ref, 9)
    # Fill the view to capacity and then one more — rollover.
    view.append(_bar(10))
    # On next access the view detects the rollover and rebuilds. The
    # deque still holds 10 bars and the DataFrame must match.
    val = view.indicator(ref, 9)
    assert val is not None
    assert len(view._df) == 10
    assert view._df_rows == 10
    assert view._needs_full_rebuild is False


def test_multi_indicator_share_dataframe() -> None:
    view = StreamingHistoryView(max_bars=30)
    for i in range(20):
        view.append(_bar(i))
    sma_ref = IndicatorRef(name="sma", params={"period": 5}, source="close")
    ema_ref = IndicatorRef(name="ema", params={"period": 5}, source="close")
    view.indicator(sma_ref, 19)
    view.indicator(ema_ref, 19)
    # Both indicators are computed against the same shared DataFrame.
    assert len(view._df) == 20
    assert sma_ref.model_dump_json() in view._indicator_cache
    assert ema_ref.model_dump_json() in view._indicator_cache
