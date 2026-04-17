"""Unit tests for the candle resampler.

Focus: the three invariants from ``system_design/pr2_live_data_and_paper_cutover.md``
§3.4 — only finalized bars leave, monotonic timestamps, no fabricated bars in
gaps — plus the passthrough and upscale-from-native-bar paths.
"""

from __future__ import annotations

from typing import List

import pytest

from investment_team.trading_service.data_stream.protocol import BarEvent
from investment_team.trading_service.data_stream.resampler import (
    NativeBar,
    NativeTick,
    Resampler,
    timeframe_to_seconds,
)

# ---------------------------------------------------------------------------
# Timeframe parsing
# ---------------------------------------------------------------------------


def test_timeframe_to_seconds_known_values() -> None:
    assert timeframe_to_seconds("1s") == 1
    assert timeframe_to_seconds("15s") == 15
    assert timeframe_to_seconds("1m") == 60
    assert timeframe_to_seconds("15m") == 900
    assert timeframe_to_seconds("1h") == 3600
    assert timeframe_to_seconds("1d") == 86_400


def test_timeframe_to_seconds_unknown_raises() -> None:
    with pytest.raises(ValueError, match="unknown timeframe"):
        timeframe_to_seconds("42q")


# ---------------------------------------------------------------------------
# Tick path
# ---------------------------------------------------------------------------


def _tick(ts: str, price: float, size: float = 1.0, symbol: str = "BTC") -> NativeTick:
    return NativeTick(timestamp=ts, symbol=symbol, price=price, size=size)


def _collect(iterator) -> List[BarEvent]:
    return list(iterator)


def test_ticks_into_1m_bar_emits_on_next_interval_boundary() -> None:
    """A 1m bar is emitted only after a tick in the *next* minute arrives."""
    r = Resampler("1m")
    emitted: List[BarEvent] = []

    # All three ticks fall within the same minute [12:00:00, 12:01:00).
    emitted += _collect(r.feed_native(_tick("2024-05-01T12:00:05Z", 100.0, 10)))
    emitted += _collect(r.feed_native(_tick("2024-05-01T12:00:30Z", 102.0, 5)))
    emitted += _collect(r.feed_native(_tick("2024-05-01T12:00:59Z", 101.5, 3)))
    assert emitted == []  # no bar yet — minute not closed

    # The first tick of the next minute finalizes the previous bar.
    emitted += _collect(r.feed_native(_tick("2024-05-01T12:01:02Z", 103.0, 2)))
    assert len(emitted) == 1
    bar = emitted[0].bar
    assert bar.symbol == "BTC"
    assert bar.timeframe == "1m"
    assert bar.timestamp == "2024-05-01T12:01:00Z"  # close time of the 12:00 bar
    assert bar.open == 100.0
    assert bar.high == 102.0
    assert bar.low == 100.0
    assert bar.close == 101.5
    assert bar.volume == 18.0


def test_ticks_partial_bar_is_not_emitted_on_flush() -> None:
    """End-of-stream must NOT emit an in-progress bar (look-ahead safety)."""
    r = Resampler("1m")
    _collect(r.feed_native(_tick("2024-05-01T12:00:05Z", 100.0)))
    _collect(r.feed_native(_tick("2024-05-01T12:00:30Z", 101.0)))
    # No later tick arrives.
    assert _collect(r.flush_on_end()) == []


def test_ticks_gap_emits_no_fabricated_bars() -> None:
    """If a full minute passes with no tick, no placeholder bar is emitted."""
    r = Resampler("1m")
    emitted: List[BarEvent] = []
    emitted += _collect(r.feed_native(_tick("2024-05-01T12:00:05Z", 100.0)))
    # Skip 12:01 entirely; next tick is at 12:02:05.
    emitted += _collect(r.feed_native(_tick("2024-05-01T12:02:05Z", 110.0)))
    # Only one bar emitted: the 12:00 bar, finalized by the 12:02 tick.
    # The 12:01 gap is preserved as absence, not a zero-volume bar.
    assert len(emitted) == 1
    assert emitted[0].bar.timestamp == "2024-05-01T12:01:00Z"
    assert emitted[0].bar.close == 100.0


def test_out_of_order_tick_is_dropped_and_counted() -> None:
    r = Resampler("1m")
    # Advance past 12:00 — first bar finalized by a 12:01 tick.
    _collect(r.feed_native(_tick("2024-05-01T12:00:05Z", 100.0)))
    emitted = _collect(r.feed_native(_tick("2024-05-01T12:01:30Z", 101.0)))
    assert len(emitted) == 1

    # Now send a late print from 12:00 — must be dropped.
    late = _collect(r.feed_native(_tick("2024-05-01T12:00:45Z", 999.0)))
    assert late == []
    assert r.stats.out_of_order_dropped == 1


def test_multi_symbol_ticks_are_independent() -> None:
    r = Resampler("1m")
    emitted: List[BarEvent] = []
    emitted += _collect(r.feed_native(_tick("2024-05-01T12:00:05Z", 100.0, symbol="BTC")))
    emitted += _collect(r.feed_native(_tick("2024-05-01T12:00:10Z", 50.0, symbol="ETH")))
    emitted += _collect(r.feed_native(_tick("2024-05-01T12:01:01Z", 101.0, symbol="BTC")))
    # Only BTC's 12:00 bar is done — ETH's isn't (no later ETH tick yet).
    assert len(emitted) == 1
    assert emitted[0].bar.symbol == "BTC"

    # ETH later tick finalizes ETH's bar.
    emitted += _collect(r.feed_native(_tick("2024-05-01T12:01:02Z", 51.0, symbol="ETH")))
    assert len(emitted) == 2
    assert emitted[1].bar.symbol == "ETH"


def test_ticks_resampled_to_15m() -> None:
    r = Resampler("15m")
    # Three ticks inside [12:00, 12:15), then one in the next interval.
    _collect(r.feed_native(_tick("2024-05-01T12:00:00Z", 100.0, 10)))
    _collect(r.feed_native(_tick("2024-05-01T12:07:30Z", 95.0, 5)))  # low
    _collect(r.feed_native(_tick("2024-05-01T12:14:59Z", 110.0, 3)))  # high then close?
    # Tick order determines "last price" — close is last tick's price.
    emitted = _collect(r.feed_native(_tick("2024-05-01T12:15:10Z", 112.0, 1)))
    assert len(emitted) == 1
    bar = emitted[0].bar
    assert bar.timestamp == "2024-05-01T12:15:00Z"
    assert bar.open == 100.0
    assert bar.high == 110.0
    assert bar.low == 95.0
    assert bar.close == 110.0  # last tick IN the interval
    assert bar.volume == 18.0


# ---------------------------------------------------------------------------
# Native-bar passthrough (native == target)
# ---------------------------------------------------------------------------


def _native_bar(
    close_ts: str,
    tf: str,
    o: float,
    h: float,
    low_: float,
    c: float,
    vol: float = 0.0,
    symbol: str = "BTC",
) -> NativeBar:
    return NativeBar(
        timestamp=close_ts,
        symbol=symbol,
        timeframe=tf,
        open=o,
        high=h,
        low=low_,
        close=c,
        volume=vol,
    )


def test_native_bar_passthrough_when_tf_matches_target() -> None:
    r = Resampler("1m")
    emitted = _collect(
        r.feed_native(_native_bar("2024-05-01T12:01:00Z", "1m", 100, 102, 99, 101, 500))
    )
    assert len(emitted) == 1
    bar = emitted[0].bar
    assert bar.timestamp == "2024-05-01T12:01:00Z"
    assert bar.timeframe == "1m"
    assert bar.close == 101
    assert bar.volume == 500


def test_native_bar_coarser_than_target_raises() -> None:
    r = Resampler("1m")
    with pytest.raises(ValueError, match="coarser than target"):
        _collect(r.feed_native(_native_bar("2024-05-01T12:05:00Z", "5m", 100, 102, 99, 101)))


# ---------------------------------------------------------------------------
# Upscale (native < target)
# ---------------------------------------------------------------------------


def test_upscale_1m_native_bars_into_5m() -> None:
    """Five 1m native bars fold into one 5m target bar."""
    r = Resampler("5m")
    emitted: List[BarEvent] = []

    # 12:01 close covers [12:00, 12:01)
    emitted += _collect(
        r.feed_native(_native_bar("2024-05-01T12:01:00Z", "1m", 100, 101, 99, 100.5, 10))
    )
    emitted += _collect(
        r.feed_native(_native_bar("2024-05-01T12:02:00Z", "1m", 100.5, 103, 100, 102, 20))
    )
    emitted += _collect(
        r.feed_native(_native_bar("2024-05-01T12:03:00Z", "1m", 102, 104, 101, 103, 15))
    )
    emitted += _collect(
        r.feed_native(_native_bar("2024-05-01T12:04:00Z", "1m", 103, 103.5, 102, 102.5, 5))
    )
    # Fifth bar closes at 12:05:00 — exactly on the 5m boundary. Should
    # finalize eagerly.
    emitted += _collect(
        r.feed_native(_native_bar("2024-05-01T12:05:00Z", "1m", 102.5, 105, 102, 104.5, 25))
    )

    assert len(emitted) == 1
    bar = emitted[0].bar
    assert bar.timestamp == "2024-05-01T12:05:00Z"
    assert bar.timeframe == "5m"
    assert bar.open == 100  # first-native open
    assert bar.high == 105
    assert bar.low == 99
    assert bar.close == 104.5  # last-native close
    assert bar.volume == 75  # sum


def test_upscale_with_gap_does_not_fabricate_bars() -> None:
    r = Resampler("5m")
    emitted: List[BarEvent] = []
    # Two 1m bars in [12:00, 12:05)...
    emitted += _collect(
        r.feed_native(_native_bar("2024-05-01T12:01:00Z", "1m", 100, 101, 99, 100.5, 10))
    )
    emitted += _collect(
        r.feed_native(_native_bar("2024-05-01T12:02:00Z", "1m", 100.5, 102, 100, 101, 5))
    )
    # Gap: no bars for 12:02-12:05. Next bar arrives in [12:05, 12:10).
    emitted += _collect(
        r.feed_native(_native_bar("2024-05-01T12:06:00Z", "1m", 110, 111, 109, 110.5, 20))
    )
    # The [12:00, 12:05) 5m bar is now finalized (because 12:06 > 12:05).
    # No bars for the gap should appear.
    assert len(emitted) == 1
    assert emitted[0].bar.timestamp == "2024-05-01T12:05:00Z"
    assert emitted[0].bar.close == 101
    # The new partial (for [12:05, 12:10)) is not yet emitted.


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


def test_stats_count_bars_and_symbols() -> None:
    r = Resampler("1m")
    _collect(r.feed_native(_tick("2024-05-01T12:00:05Z", 100.0, symbol="BTC")))
    _collect(
        r.feed_native(_tick("2024-05-01T12:01:05Z", 101.0, symbol="BTC"))
    )  # finalizes BTC 12:00
    _collect(r.feed_native(_tick("2024-05-01T12:00:10Z", 50.0, symbol="ETH")))
    _collect(
        r.feed_native(_tick("2024-05-01T12:01:10Z", 51.0, symbol="ETH"))
    )  # finalizes ETH 12:00
    assert r.stats.bars_emitted == 2
    assert r.stats.symbols == {"BTC": 1, "ETH": 1}
