"""Historical replay stream.

Wraps ``MarketDataService`` (and, in PR 2, Polygon.io REST aggregates at the
lowest available timeframe) and emits ``BarEvent`` objects in chronological
order across all requested symbols.

The strategy only ever receives one ``Bar`` per ``on_bar`` invocation, so
the replay stream is the single place where future-vs-past sequencing is
enforced. The FillSimulator in the parent process consumes the same events
but with a one-bar forward view; that is safe because it lives outside the
strategy subprocess.
"""

from __future__ import annotations

import math
from typing import Dict, Iterator, List, Optional

from ...market_data_service import OHLCVBar
from ..strategy.contract import Bar
from .protocol import BarEvent, EndOfStreamEvent, StreamEvent


class HistoricalReplayStream:
    """Chronological bar stream built from ``{symbol: [OHLCVBar, ...]}``.

    PR 1 uses the existing ``MarketDataService`` (daily bars). In PR 2 this
    class grows a Polygon path that pulls sub-daily aggregates.
    """

    def __init__(
        self,
        market_data: Dict[str, List[OHLCVBar]],
        *,
        timeframe: str = "1d",
    ) -> None:
        self._market_data = market_data
        self._timeframe = timeframe

    # ------------------------------------------------------------------

    def __iter__(self) -> Iterator[StreamEvent]:
        # Flatten into a single timeline and sort by date.
        timeline: List[tuple[str, str, OHLCVBar]] = []
        for symbol, bars in self._market_data.items():
            for bar in bars:
                timeline.append((bar.date, symbol, bar))
        timeline.sort(key=lambda x: (x[0], x[1]))
        for ts, symbol, bar in timeline:
            yield BarEvent(
                bar=Bar(
                    symbol=symbol,
                    timestamp=ts,
                    timeframe=self._timeframe,
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    # Canonicalise a non-finite volume to 0.0 so the strategy and
                    # its predicates see the same value the cache fingerprint
                    # collapses NaN/inf to — a backtest can't behave differently
                    # for a NaN-volume dataset than for the 0.0 it shares a
                    # dataset_fingerprint with.
                    volume=bar.volume if math.isfinite(bar.volume) else 0.0,
                )
            )
        yield EndOfStreamEvent()

    # ------------------------------------------------------------------

    @classmethod
    def from_market_data_service(
        cls,
        *,
        symbols: List[str],
        asset_class: str,
        start_date: str,
        end_date: str,
        market_service: Optional[object] = None,
        timeframe: str = "1d",
    ) -> "HistoricalReplayStream":
        """Factory — lazily imports ``MarketDataService`` to keep cold start fast."""
        if market_service is None:
            from ...market_data_service import MarketDataService

            market_service = MarketDataService()
        market_data = market_service.fetch_multi_symbol_range(
            symbols, asset_class, start_date, end_date
        )
        return cls(market_data, timeframe=timeframe)
