"""Databento provider adapter — paid alt (stocks, futures, options).

Activated when ``DATABENTO_API_KEY`` is set. Institutional-grade historical
and live data; useful when users need high-fidelity fill simulation.
"""

from __future__ import annotations

import os
from typing import Iterator, List, Optional

from ..data_stream.protocol import BarEvent
from ..data_stream.resampler import NativeEvent
from .base import ProviderCapabilities

CAPABILITIES = ProviderCapabilities(
    name="databento",
    supports={"equities"},  # PR 2 scope: equities only; futures/options later
    is_paid=True,
    historical_timeframes={"1s", "1m", "5m", "15m", "30m", "1h", "1d"},
    live_timeframes={"tick", "1s", "1m"},
)


class DatabentoAdapter:
    capabilities = CAPABILITIES

    def __init__(self) -> None:
        self._api_key = os.environ.get("DATABENTO_API_KEY")

    def _require_auth(self) -> None:
        if not self._api_key:
            raise RuntimeError("databento adapter requires DATABENTO_API_KEY")

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
        raise NotImplementedError("databento historical pump is not yet wired")
        yield  # pragma: no cover

    def live(
        self,
        *,
        symbols: List[str],
        asset_class: str,
        native_timeframe: str,
    ) -> Iterator[NativeEvent]:
        self._require_auth()
        raise NotImplementedError("databento live pump is not yet wired")
        yield  # pragma: no cover


def build() -> DatabentoAdapter:
    return DatabentoAdapter()


__all__ = ["CAPABILITIES", "DatabentoAdapter", "build"]
