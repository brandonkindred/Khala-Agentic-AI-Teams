"""TradingView MCP data source for the Strategy Lab.

This package lets the Strategy Lab's :class:`MarketDataService` pull OHLCV bars from a
user-configured TradingView MCP server. Configuration is owned by the Unified API
Integrations surface (``/api/integrations/tradingview``); :func:`resolve_tradingview_mcp_config`
reads it (with an environment-variable override) so the team stays importable
standalone, and :class:`TradingViewMcpClient` speaks streamable-HTTP JSON-RPC to the
configured server.
"""

from __future__ import annotations

from .client import TradingViewMcpClient, TradingViewMcpError
from .config import TradingViewMcpConfig, resolve_tradingview_mcp_config

__all__ = [
    "TradingViewMcpClient",
    "TradingViewMcpConfig",
    "TradingViewMcpError",
    "resolve_tradingview_mcp_config",
]
