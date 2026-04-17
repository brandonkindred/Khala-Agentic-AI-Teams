"""Coinbase Exchange provider adapter — crypto secondary default.

Selected automatically when the Binance primary raises
:class:`ProviderRegionBlocked` at session open (see registry's
``resolve_live``). Free and keyless, like Binance, but with a narrower
native-timeframe menu.

REST: ``https://api.exchange.coinbase.com/products/{pair}/candles``.
Live WS: ``wss://ws-feed.exchange.coinbase.com`` (``matches`` channel for
ticks, ``ticker`` for quotes).
"""

from __future__ import annotations

import os
from typing import Iterator, List, Optional

from ..data_stream.protocol import BarEvent
from ..data_stream.resampler import NativeEvent
from .base import ProviderCapabilities

CAPABILITIES = ProviderCapabilities(
    name="coinbase",
    supports={"crypto"},
    is_paid=False,
    historical_timeframes={"1m", "5m", "15m", "1h", "6h", "1d"},
    live_timeframes={"tick", "1m"},
)


class CoinbaseAdapter:
    """Free, keyless Coinbase Exchange market-data adapter."""

    capabilities = CAPABILITIES

    def __init__(self, *, base_rest: Optional[str] = None, base_ws: Optional[str] = None) -> None:
        self._rest = base_rest or os.environ.get(
            "COINBASE_REST_URL", "https://api.exchange.coinbase.com"
        )
        self._ws = base_ws or os.environ.get(
            "COINBASE_WS_URL", "wss://ws-feed.exchange.coinbase.com"
        )

    def smallest_available(self, asset_class: str, *, live: bool) -> Optional[str]:
        if asset_class != "crypto":
            return None
        return "tick" if live else "1m"

    def historical(
        self,
        *,
        symbols: List[str],
        asset_class: str,
        start: str,
        end: str,
        timeframe: str,
    ) -> Iterator[BarEvent]:
        raise NotImplementedError(
            "coinbase historical REST pump is not yet wired; it serves as a "
            "geo-failover secondary for Binance at session open only"
        )
        yield  # pragma: no cover - generator typing marker

    def live(
        self,
        *,
        symbols: List[str],
        asset_class: str,
        native_timeframe: str,
    ) -> Iterator[NativeEvent]:
        raise NotImplementedError("coinbase live websocket pump is not yet wired")
        yield  # pragma: no cover


def build() -> CoinbaseAdapter:
    return CoinbaseAdapter()


__all__ = ["CAPABILITIES", "CoinbaseAdapter", "build"]
