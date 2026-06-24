"""Tests for the ``StreamingHistoryView`` streaming-registry backing.

The view advances a retained ``IndicatorRegistry`` one step per appended bar
and stores each indicator's trailing value in a per-``sig_id`` buffer aligned
1:1 with the bounded bars deque, so ``indicator(ref, i)`` — including the
``i``/``i-1`` reads ``cross_above``/``cross_below`` need — is a buffer lookup
with no full-window recompute. These tests pin the alignment, rollover, lazy
backfill / catch-up, and warm-up behaviours, using a fresh view seeded with the
same bar tail as the bit-exact reference.
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

    Any stale-buffer or mis-alignment regression produces a value that differs
    from this fresh-view computation on the same input tail.
    """
    fresh = StreamingHistoryView(max_bars=max(len(bars), 1))
    for b in bars:
        fresh.append(b)
    return fresh.indicator(ref, i)


def test_same_bar_repeat_query_is_idempotent() -> None:
    """Within one bar, a repeat query for the same ref neither re-syncs nor
    grows the buffer — the same-bar dedupe contract."""
    view = StreamingHistoryView(max_bars=50)
    for i in range(20):
        view.append(_bar(i))
    ref = IndicatorRef(name="ema", params={"period": 5}, source="close")
    first = view.indicator(ref, 19)
    st = view._buffers[ref.sig_id]
    assert st["synced"] == view._append_counter == 20
    buf_len = len(st["buf"])
    second = view.indicator(ref, 19)
    assert second == first
    assert st["synced"] == 20
    assert len(st["buf"]) == buf_len


def test_append_advances_buffer_on_next_access() -> None:
    """An append leaves the buffer behind; the next access advances it and
    returns a value reflecting the new bar."""
    view = StreamingHistoryView(max_bars=50)
    for i in range(20):
        view.append(_bar(i))
    ref = IndicatorRef(name="sma", params={"period": 5}, source="close")
    v1 = view.indicator(ref, 19)
    assert v1 is not None
    view.append(_bar(20))
    v2 = view.indicator(ref, 20)
    assert v2 is not None
    assert len(view._buffers[ref.sig_id]["buf"]) == 21
    assert v2 != v1  # SMA window slid forward by one bar


def test_buffer_aligned_with_rotated_deque_after_rollover() -> None:
    """Appending past max_bars drops the oldest bar; the buffer rolls in
    lockstep so ``buf[i]`` still tracks ``bars[i]`` in the rotated deque."""
    view = StreamingHistoryView(max_bars=10)
    for i in range(10):
        view.append(_bar(i))
    ref = IndicatorRef(name="sma", params={"period": 3}, source="close")
    pre = view.indicator(ref, 9)
    assert pre is not None
    view.append(_bar(10))  # deque pops bar 0, appends bar 10 (a slide)
    val = view.indicator(ref, 9)
    assert val is not None
    assert len(view._buffers[ref.sig_id]["buf"]) == 10
    # buf[9] now tracks SMA over bars [8, 9, 10], not the pre-rollover [7, 8, 9].
    assert val != pre
    assert val == _fresh_view_value([_bar(j) for j in range(1, 11)], ref, 9)


def test_cross_reads_trailing_and_previous_bar() -> None:
    """Both the i and i-1 reads cross_above/cross_below need are addressable
    from the buffer and agree with a fresh view."""
    view = StreamingHistoryView(max_bars=50)
    for i in range(20):
        view.append(_bar(i))
    ref = IndicatorRef(name="sma", params={"period": 3}, source="close")
    trailing = view.indicator(ref, 19)
    prev = view.indicator(ref, 18)
    assert trailing is not None and prev is not None
    bars = [_bar(j) for j in range(20)]
    assert prev == _fresh_view_value(bars, ref, 18)
    assert trailing == _fresh_view_value(bars, ref, 19)


def test_repeated_appends_match_fresh_view_value() -> None:
    """After every append, the live trailing value must equal a fresh view
    seeded with the same trailing window, AND the counter advances one per
    append (defends an off-by-one in the bump)."""
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
        # The view holds the trailing 5 bars (deque maxlen=5).
        expected = _fresh_view_value([_bar(j) for j in range(i - 4, i + 1)], ref, 4)
        assert live == expected, f"i={i} live={live!r} expected={expected!r}"


def test_batched_appends_then_catch_up() -> None:
    """Appends with no intervening ``indicator()`` leave the buffer's synced
    watermark behind; the next query rebuilds against the live (rolled-over)
    deque rather than reading a stale value."""
    ref = IndicatorRef(name="sma", params={"period": 3}, source="close")
    view = StreamingHistoryView(max_bars=5)
    for i in range(5):
        view.append(_bar(i))
    view.indicator(ref, 4)  # seed the buffer
    assert view._buffers[ref.sig_id]["synced"] == view._append_counter == 5
    # Batched appends with NO intervening indicator() calls (drops bars 0..14).
    for i in range(5, 20):
        view.append(_bar(i))
    assert view._append_counter == 20
    assert view._buffers[ref.sig_id]["synced"] == 5, (
        "synced must NOT advance silently — indicator() was never called"
    )
    live = view.indicator(ref, 4)
    expected = _fresh_view_value([_bar(j) for j in range(15, 20)], ref, 4)
    assert live == expected, f"stale batched-append buffer: live={live!r} expected={expected!r}"
    assert view._buffers[ref.sig_id]["synced"] == 20


def test_lazy_first_registration_mid_stream_backfills() -> None:
    """A ref first queried after many bars have streamed backfills its buffer
    so historical indices (i-1, etc.) are addressable and aligned."""
    view = StreamingHistoryView(max_bars=50)
    for i in range(30):
        view.append(_bar(i))
    ref = IndicatorRef(name="sma", params={"period": 5}, source="close")
    v_last = view.indicator(ref, 29)  # first ever query for this ref
    v_prev = view.indicator(ref, 28)
    assert len(view._buffers[ref.sig_id]["buf"]) == 30
    bars = [_bar(j) for j in range(30)]
    assert v_last == _fresh_view_value(bars, ref, 29)
    assert v_prev == _fresh_view_value(bars, ref, 28)


def test_indicator_returns_none_on_empty_view() -> None:
    """An empty view returns ``None`` instead of indexing an empty buffer."""
    view = StreamingHistoryView()
    ref = IndicatorRef(name="sma", params={"period": 5}, source="close")
    assert view.indicator(ref, 0) is None


def test_warmup_returns_none_until_enough_history() -> None:
    view = StreamingHistoryView(max_bars=50)
    ref = IndicatorRef(name="sma", params={"period": 5}, source="close")
    for i in range(4):
        view.append(_bar(i))
    assert view.indicator(ref, 3) is None  # < period
    view.append(_bar(4))
    assert view.indicator(ref, 4) is not None  # period reached


def test_out_of_range_index_returns_none() -> None:
    view = StreamingHistoryView(max_bars=50)
    for i in range(10):
        view.append(_bar(i))
    ref = IndicatorRef(name="sma", params={"period": 3}, source="close")
    assert view.indicator(ref, 10) is None  # i >= len(buf)
    assert view.indicator(ref, -1) is None


def test_independent_buffers_per_indicator() -> None:
    view = StreamingHistoryView(max_bars=30)
    for i in range(20):
        view.append(_bar(i))
    sma_ref = IndicatorRef(name="sma", params={"period": 5}, source="close")
    ema_ref = IndicatorRef(name="ema", params={"period": 5}, source="close")
    assert view.indicator(sma_ref, 19) is not None
    assert view.indicator(ema_ref, 19) is not None
    assert sma_ref.sig_id in view._buffers
    assert ema_ref.sig_id in view._buffers
    assert view._buffers[sma_ref.sig_id]["buf"] is not view._buffers[ema_ref.sig_id]["buf"]
    # One shared bars_list snapshot served both refs on the same bar.
    assert view._bars_list_counter == view._append_counter == 20
