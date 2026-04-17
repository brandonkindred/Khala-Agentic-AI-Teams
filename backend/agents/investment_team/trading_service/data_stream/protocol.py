"""Market data stream protocol.

The Trading Service consumes an iterable of ``StreamEvent`` records. The
concrete sources — :class:`HistoricalReplayStream` in PR 1, the Polygon
websocket stream in PR 2 — implement the same iterator contract so the
engine itself is mode-agnostic.
"""

from __future__ import annotations

from typing import Iterable, Iterator, Protocol, Union

from pydantic import BaseModel

from ..strategy.contract import Bar


class BarEvent(BaseModel):
    """A single finalized candle destined for the strategy.

    When ``is_warmup`` is True, the service delivers the bar to the strategy
    with ``ctx.is_warmup = True`` and suppresses fill simulation for that
    bar — warm-up bars populate indicator history without side effects.
    """

    bar: Bar
    is_warmup: bool = False


class EndOfStreamEvent(BaseModel):
    """Emitted once, after the last real event."""

    reason: str = "end_of_data"


StreamEvent = Union[BarEvent, EndOfStreamEvent]


class MarketDataStream(Protocol):
    """Iterable contract — implementations yield StreamEvents chronologically."""

    def __iter__(self) -> Iterator[StreamEvent]: ...


def as_bar_events(bars: Iterable[Bar]) -> Iterator[StreamEvent]:
    """Utility: wrap a plain iterable of Bars into the StreamEvent protocol."""
    for b in bars:
        yield BarEvent(bar=b)
    yield EndOfStreamEvent()
