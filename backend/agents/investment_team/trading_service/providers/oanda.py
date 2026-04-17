"""OANDA v20 provider adapter — FX default (free practice account).

Requires ``OANDA_API_TOKEN`` and ``OANDA_ACCOUNT_ID`` (free practice-account
signup). See ``system_design/pr2_live_data_and_paper_cutover.md`` §2.2.

REST candles: ``GET /v3/instruments/{instrument}/candles``.
Streaming prices: ``GET /v3/accounts/{accountID}/pricing/stream``.
"""

from __future__ import annotations

import os
from typing import Iterator, List, Optional

from ..data_stream.protocol import BarEvent
from ..data_stream.resampler import NativeEvent
from .base import ProviderCapabilities

CAPABILITIES = ProviderCapabilities(
    name="oanda",
    supports={"fx"},
    is_paid=False,
    historical_timeframes={"5s", "15s", "30s", "1m", "5m", "15m", "30m", "1h", "4h", "1d"},
    live_timeframes={"tick"},
)


class OandaAdapter:
    capabilities = CAPABILITIES

    def __init__(self) -> None:
        self._token = os.environ.get("OANDA_API_TOKEN")
        self._account_id = os.environ.get("OANDA_ACCOUNT_ID")

    def _require_auth(self) -> None:
        if not self._token or not self._account_id:
            raise RuntimeError(
                "oanda adapter requires OANDA_API_TOKEN and OANDA_ACCOUNT_ID "
                "(free practice signup at https://www.oanda.com/demo-account/). "
                "FX paper trading cannot proceed without a configured FX provider."
            )

    def smallest_available(self, asset_class: str, *, live: bool) -> Optional[str]:
        if asset_class != "fx":
            return None
        return "tick" if live else "5s"

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
        raise NotImplementedError("oanda historical REST pump is not yet wired")
        yield  # pragma: no cover

    def live(
        self,
        *,
        symbols: List[str],
        asset_class: str,
        native_timeframe: str,
    ) -> Iterator[NativeEvent]:
        self._require_auth()
        raise NotImplementedError("oanda pricing stream pump is not yet wired")
        yield  # pragma: no cover


def build() -> OandaAdapter:
    return OandaAdapter()


__all__ = ["CAPABILITIES", "OandaAdapter", "build"]
