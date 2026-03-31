"""Protocol for Strategy Lab market data providers."""

from __future__ import annotations

from typing import Protocol

from .models import MarketLabContext, StrategyLabDataRequest


class StrategyLabMarketDataProvider(Protocol):
    def fetch_context(self, request: StrategyLabDataRequest) -> MarketLabContext:
        """Return a snapshot for the given request (may be degraded)."""
        ...
