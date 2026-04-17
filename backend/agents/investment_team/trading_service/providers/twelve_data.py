"""Twelve Data provider adapter — paid alt (stocks, FX, crypto).

Activated when ``TWELVE_DATA_API_KEY`` is set. A free tier exists but is
rate-limited to 8 calls/minute, which is not usable for streaming; we only
activate this adapter when the Pro (paid) plan key is present
(``TWELVE_DATA_PLAN=pro``).
"""

from __future__ import annotations

import os
from typing import Iterator, List, Optional

from ..data_stream.protocol import BarEvent
from ..data_stream.resampler import NativeEvent
from .base import ProviderCapabilities

CAPABILITIES = ProviderCapabilities(
    name="twelve_data",
    supports={"crypto", "equities", "fx"},
    is_paid=True,
    historical_timeframes={"1m", "5m", "15m", "30m", "1h", "4h", "1d"},
    live_timeframes={"1m"},  # no true tick stream on standard plans
    # Pumps are stubbed — flipped to True when the Twelve Data Pro
    # client is wired up.
    implemented=False,
)


class TwelveDataAdapter:
    capabilities = CAPABILITIES

    def __init__(self) -> None:
        self._api_key = os.environ.get("TWELVE_DATA_API_KEY")
        self._plan = os.environ.get("TWELVE_DATA_PLAN", "free").lower()

    def _require_auth(self) -> None:
        if not self._api_key:
            raise RuntimeError("twelve_data adapter requires TWELVE_DATA_API_KEY")
        if self._plan != "pro":
            raise RuntimeError(
                "twelve_data adapter only operates on the Pro plan "
                "(TWELVE_DATA_PLAN=pro) due to free-tier rate limits"
            )

    def smallest_available(self, asset_class: str, *, live: bool) -> Optional[str]:
        if asset_class not in self.capabilities.supports:
            return None
        return "1m"

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
        raise NotImplementedError("twelve_data historical pump is not yet wired")
        yield  # pragma: no cover

    def live(
        self,
        *,
        symbols: List[str],
        asset_class: str,
        native_timeframe: str,
    ) -> Iterator[NativeEvent]:
        self._require_auth()
        raise NotImplementedError("twelve_data live pump is not yet wired")
        yield  # pragma: no cover


def build() -> TwelveDataAdapter:
    return TwelveDataAdapter()


__all__ = ["CAPABILITIES", "TwelveDataAdapter", "build"]
