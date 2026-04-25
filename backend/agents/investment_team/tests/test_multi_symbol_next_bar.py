"""Multi-symbol next_bar gating regression tests for ``TradingService``.

Issue #248 review feedback (PR #355) flagged that the realistic execution
model's ``next_bar`` lookahead was being satisfied by the very next
``BarEvent`` in the stream regardless of symbol. In a multi-symbol
``HistoricalReplayStream`` the chronologically-next event is frequently a
different symbol, so symbol A's adverse-selection haircut would compute
against symbol B's price move.

The fix in ``service.py`` only sets ``next_bar`` when the peeked bar's
symbol equals ``cur_bar.symbol``. These tests exercise that contract via
the same ``HistoricalReplayStream`` and the same peek algorithm the
service uses — asserting that:

* Bars from different symbols *do* interleave in the stream (so the bug
  could occur).
* Re-implementing the service's peek gating yields a same-symbol
  ``next_bar`` (or ``None``), never a cross-symbol one.

A full ``TradingService.run()`` integration test would need the strategy
subprocess harness; this narrower unit test pins the algorithm.
"""

from __future__ import annotations

from typing import List, Optional

from investment_team.market_data_service import OHLCVBar
from investment_team.trading_service.data_stream.historical_replay import (
    HistoricalReplayStream,
)
from investment_team.trading_service.data_stream.protocol import (
    BarEvent,
    EndOfStreamEvent,
)
from investment_team.trading_service.strategy.contract import Bar


def _bars(symbol: str, dates: List[str]) -> List[OHLCVBar]:
    return [
        OHLCVBar(
            date=d,
            open=100.0 + i,
            high=101.0 + i,
            low=99.0 + i,
            close=100.5 + i,
            volume=1_000_000.0,
        )
        for i, d in enumerate(dates)
    ]


def _peek_next_bar_same_symbol(event_iter, cur_bar: Bar):
    """Mirror of ``TradingService.run()``'s post-#248-fix peek logic.

    Returns ``(peeked_event, next_bar)`` where ``next_bar`` is the
    peeked bar iff its symbol matches ``cur_bar.symbol``, else
    ``None``. The peeked event is preserved for the caller to replay
    on the next iteration.
    """
    next_bar: Optional[Bar] = None
    while True:
        peeked = next(event_iter, None)
        if peeked is None or isinstance(peeked, EndOfStreamEvent):
            return peeked, next_bar
        if isinstance(peeked, BarEvent):
            if peeked.bar.symbol == cur_bar.symbol:
                next_bar = peeked.bar
            return peeked, next_bar


def test_two_symbol_stream_interleaves_bars():
    """Sanity check: HistoricalReplayStream interleaves bars across
    symbols by date — proves the bug surface exists."""
    market_data = {
        "AAA": _bars("AAA", ["2024-01-02", "2024-01-03", "2024-01-04"]),
        "BBB": _bars("BBB", ["2024-01-02", "2024-01-03", "2024-01-04"]),
    }
    events = list(HistoricalReplayStream(market_data, timeframe="1d"))
    bar_events = [e for e in events if isinstance(e, BarEvent)]
    assert len(bar_events) == 6
    # Stream is sorted by (date, symbol); for any pair on the same date
    # the next bar is the *other* symbol — exactly the case the bug
    # would have mis-routed.
    symbols = [e.bar.symbol for e in bar_events]
    assert symbols == ["AAA", "BBB", "AAA", "BBB", "AAA", "BBB"]


def test_peek_returns_none_when_next_event_is_other_symbol():
    """Mid-stream AAA fill: the next event is BBB. The fix must return
    ``next_bar=None``, not BBB's bar."""
    market_data = {
        "AAA": _bars("AAA", ["2024-01-02", "2024-01-03"]),
        "BBB": _bars("BBB", ["2024-01-02", "2024-01-03"]),
    }
    event_iter = iter(HistoricalReplayStream(market_data, timeframe="1d"))

    # Walk to the first AAA bar.
    first = next(event_iter)
    assert isinstance(first, BarEvent) and first.bar.symbol == "AAA"
    cur_bar = first.bar

    peeked, next_bar = _peek_next_bar_same_symbol(event_iter, cur_bar)
    assert isinstance(peeked, BarEvent)
    assert peeked.bar.symbol == "BBB"
    # The bug would have returned ``next_bar = peeked.bar`` here. The fix
    # gates on ``peeked.bar.symbol == cur_bar.symbol``, so next_bar is None.
    assert next_bar is None


def test_peek_returns_same_symbol_bar_when_aligned():
    """Single-symbol stream — peek must still return the next bar
    (regression: don't over-zealously filter)."""
    market_data = {"AAA": _bars("AAA", ["2024-01-02", "2024-01-03", "2024-01-04"])}
    event_iter = iter(HistoricalReplayStream(market_data, timeframe="1d"))

    first = next(event_iter)
    assert isinstance(first, BarEvent)
    cur_bar = first.bar

    _, next_bar = _peek_next_bar_same_symbol(event_iter, cur_bar)
    assert next_bar is not None
    assert next_bar.symbol == "AAA"
    assert next_bar.timestamp != cur_bar.timestamp


def test_peek_handles_end_of_stream_without_next_bar():
    """Last bar in the stream — peek consumes the EndOfStream marker and
    returns ``next_bar=None``."""
    market_data = {"AAA": _bars("AAA", ["2024-01-02"])}
    event_iter = iter(HistoricalReplayStream(market_data, timeframe="1d"))

    first = next(event_iter)
    assert isinstance(first, BarEvent)
    cur_bar = first.bar

    peeked, next_bar = _peek_next_bar_same_symbol(event_iter, cur_bar)
    # End-of-stream returns the EndOfStreamEvent (or None on a re-pump);
    # next_bar is None either way.
    assert isinstance(peeked, EndOfStreamEvent) or peeked is None
    assert next_bar is None
