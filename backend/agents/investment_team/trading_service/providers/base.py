"""Provider adapter Protocol and capability descriptor.

All concrete adapters (Binance, Coinbase, Alpaca, OANDA, Polygon, Databento,
Twelve Data) implement this shape. The :class:`LiveStream` and
:class:`HistoricalReplayStream` builders depend only on this Protocol, so
swapping providers does not touch engine code.

See ``system_design/pr2_live_data_and_paper_cutover.md`` §3.2.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Optional, Protocol, Set, runtime_checkable

from ..data_stream.protocol import BarEvent
from ..data_stream.resampler import NativeEvent


class ProviderRegionBlocked(Exception):
    """Raised by an adapter when the current host region is not served.

    The registry uses this at session-open time to trigger geo-failover
    (currently Binance → Coinbase for crypto). It is **not** raised
    mid-session; mid-session failures terminate the session via
    :class:`ProviderError` so the "one adapter per session" invariant is
    preserved.
    """


class ProviderError(Exception):
    """Unrecoverable adapter error during streaming."""


@dataclass
class ProviderCapabilities:
    """What a given adapter supports. Plain data, no behavior."""

    #: Stable id used in API responses and env-var overrides (e.g. ``"binance"``).
    name: str
    #: Subset of ``{"crypto", "equities", "fx"}`` this adapter can serve.
    supports: Set[str] = field(default_factory=set)
    #: True if the adapter is a *paid* provider (requires an API key to
    #: function). The registry prefers paid adapters for the asset classes
    #: they cover when a key is configured.
    is_paid: bool = False
    #: Allowed-list of native timeframes the adapter exposes on its
    #: historical endpoint. ``"tick"`` is reserved for the live feed.
    historical_timeframes: Set[str] = field(default_factory=set)
    #: Live-feed granularity labels the adapter can stream. ``"tick"`` means
    #: a trade/quote stream the resampler will turn into 1s+ candles.
    live_timeframes: Set[str] = field(default_factory=set)


@runtime_checkable
class ProviderAdapter(Protocol):
    """Adapter contract. All methods are synchronous iterators.

    Adapters are free to implement ``historical`` only, ``live`` only, or
    both. The registry records which directions each adapter supports and
    excludes it from selection for unsupported directions.
    """

    capabilities: ProviderCapabilities

    def smallest_available(self, asset_class: str, *, live: bool) -> Optional[str]:
        """Return the shortest timeframe (or ``"tick"``) the adapter offers.

        ``None`` means the adapter has no feed for that asset class in that
        direction. Used by ``LiveStream`` / ``HistoricalReplayStream`` to
        pick a native subscription before handing to the resampler.
        """
        ...

    def historical(
        self,
        *,
        symbols: list[str],
        asset_class: str,
        start: str,
        end: str,
        timeframe: str,
    ) -> Iterator[BarEvent]:
        """Historical replay source. Emits ``BarEvent`` in chronological order."""
        ...

    def live(
        self,
        *,
        symbols: list[str],
        asset_class: str,
        native_timeframe: str,
    ) -> Iterator[NativeEvent]:
        """Live feed. Emits native ticks / bars that the resampler consumes.

        Raises :class:`ProviderRegionBlocked` at *start-of-stream* if this
        host is not permitted by the provider. Raises :class:`ProviderError`
        for any other unrecoverable error (including mid-stream).
        """
        ...


__all__ = [
    "ProviderAdapter",
    "ProviderCapabilities",
    "ProviderError",
    "ProviderRegionBlocked",
]
