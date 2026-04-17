"""Provider-backed historical stream.

Thin adapter that iterates a :class:`ProviderAdapter`'s ``historical()``
method and appends the :class:`EndOfStreamEvent` sentinel, so that a
historical feed resolved through the registry can drive the same
:class:`TradingService.run` that ``HistoricalReplayStream`` drives.

Used by :func:`run_backtest` when the caller provides ``(symbols,
asset_class)`` instead of a pre-fetched market-data dict — this is the
path that unlocks sub-daily backtests without any changes to
``MarketDataService``.
"""

from __future__ import annotations

from typing import Iterator, List

from ..providers.base import ProviderAdapter
from .protocol import BarEvent, EndOfStreamEvent, StreamEvent


class ProviderHistoricalStream:
    """Iterate a provider's historical endpoint as a ``StreamEvent`` sequence."""

    def __init__(
        self,
        *,
        provider: ProviderAdapter,
        symbols: List[str],
        asset_class: str,
        start: str,
        end: str,
        timeframe: str,
    ) -> None:
        self._provider = provider
        self._symbols = symbols
        self._asset_class = asset_class
        self._start = start
        self._end = end
        self._timeframe = timeframe

    def __iter__(self) -> Iterator[StreamEvent]:
        for event in self._provider.historical(
            symbols=self._symbols,
            asset_class=self._asset_class,
            start=self._start,
            end=self._end,
            timeframe=self._timeframe,
        ):
            # Providers already yield BarEvent — pass through unchanged.
            if isinstance(event, BarEvent):
                yield event
            else:  # pragma: no cover - defensive
                # If a provider ever starts emitting other event kinds, the
                # engine would silently ignore them. Surface that here so it
                # shows up in tests.
                raise TypeError(
                    f"provider returned non-BarEvent during historical stream: "
                    f"{type(event).__name__}"
                )
        yield EndOfStreamEvent()


__all__ = ["ProviderHistoricalStream"]
