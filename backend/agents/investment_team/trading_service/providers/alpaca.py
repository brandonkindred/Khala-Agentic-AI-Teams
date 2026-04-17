"""Alpaca provider adapter — equities default (free IEX feed).

Free tier requires a keyless-but-registered signup (``ALPACA_API_KEY_ID`` /
``ALPACA_API_SECRET_KEY``). Set ``ALPACA_PAID_FEED=sip`` to upgrade to the
paid full-tape SIP feed — same code path, different entitlement.

REST: ``https://data.alpaca.markets/v2/stocks/bars``.
Live WS: ``wss://stream.data.alpaca.markets/v2/{iex|sip}``.
"""

from __future__ import annotations

import os
from typing import Iterator, List, Optional

from ..data_stream.protocol import BarEvent
from ..data_stream.resampler import NativeEvent
from .base import ProviderCapabilities

CAPABILITIES = ProviderCapabilities(
    name="alpaca",
    supports={"equities"},
    is_paid=False,  # the free IEX feed is the default; paid SIP is an entitlement toggle
    historical_timeframes={"1m", "5m", "15m", "1h", "1d"},
    live_timeframes={"tick", "1m"},
)


class AlpacaAdapter:
    capabilities = CAPABILITIES

    def __init__(self) -> None:
        self._key_id = os.environ.get("ALPACA_API_KEY_ID")
        self._secret = os.environ.get("ALPACA_API_SECRET_KEY")
        self._feed = os.environ.get("ALPACA_PAID_FEED", "iex").lower()
        if self._feed not in {"iex", "sip"}:
            raise ValueError(f"ALPACA_PAID_FEED must be 'iex' or 'sip', got {self._feed!r}")

    def smallest_available(self, asset_class: str, *, live: bool) -> Optional[str]:
        if asset_class != "equities":
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
        if not self._key_id or not self._secret:
            raise RuntimeError(
                "alpaca adapter requires ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY "
                "(free signup). See docs/providers/alpaca.md for setup."
            )
        raise NotImplementedError("alpaca historical REST pump is not yet wired")
        yield  # pragma: no cover

    def live(
        self,
        *,
        symbols: List[str],
        asset_class: str,
        native_timeframe: str,
    ) -> Iterator[NativeEvent]:
        if not self._key_id or not self._secret:
            raise RuntimeError(
                "alpaca adapter requires ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY "
                "(free signup). See docs/providers/alpaca.md for setup."
            )
        raise NotImplementedError(f"alpaca live websocket pump ({self._feed}) is not yet wired")
        yield  # pragma: no cover


def build() -> AlpacaAdapter:
    return AlpacaAdapter()


__all__ = ["AlpacaAdapter", "CAPABILITIES", "build"]
