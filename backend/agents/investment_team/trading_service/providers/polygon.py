"""Polygon.io provider adapter — paid alt (stocks, options, crypto, forex).

Activated when ``POLYGON_API_KEY`` is set. When active, the registry prefers
Polygon over the free defaults for every asset class Polygon covers.
"""

from __future__ import annotations

import os
from typing import Iterator, List, Optional

from ..data_stream.protocol import BarEvent
from ..data_stream.resampler import NativeEvent
from .base import ProviderCapabilities

CAPABILITIES = ProviderCapabilities(
    name="polygon",
    supports={"crypto", "equities", "fx"},
    is_paid=True,
    historical_timeframes={"1s", "1m", "5m", "15m", "30m", "1h", "4h", "1d"},
    live_timeframes={"tick", "1s", "1m"},
)


class PolygonAdapter:
    capabilities = CAPABILITIES

    def __init__(self) -> None:
        self._api_key = os.environ.get("POLYGON_API_KEY")

    def _require_auth(self) -> None:
        if not self._api_key:
            raise RuntimeError("polygon adapter requires POLYGON_API_KEY")

    def smallest_available(self, asset_class: str, *, live: bool) -> Optional[str]:
        if asset_class not in self.capabilities.supports:
            return None
        return "tick" if live else "1s"

    def historical(
        self,
        *,
        symbols: List[str],
        asset_class: str,
        start: str,
        end: str,
        timeframe: str,
    ) -> Iterator[BarEvent]:
        self._require_auth()
        raise NotImplementedError("polygon historical REST pump is not yet wired")
        yield  # pragma: no cover

    def live(
        self,
        *,
        symbols: List[str],
        asset_class: str,
        native_timeframe: str,
    ) -> Iterator[NativeEvent]:
        self._require_auth()
        raise NotImplementedError("polygon live websocket pump is not yet wired")
        yield  # pragma: no cover


def build() -> PolygonAdapter:
    return PolygonAdapter()


__all__ = ["CAPABILITIES", "PolygonAdapter", "build"]
