"""Bridge the TradingView MCP client into the Strategy Lab market-data provider chain."""

from __future__ import annotations

import logging
import os

from .client import TradingViewMcpClient
from .config import TradingViewMcpConfig, resolve_tradingview_mcp_config

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 30.0


def _resolve_timeout() -> float:
    """Read the MCP request timeout (seconds) from the environment.

    Postconditions: returns ``TRADINGVIEW_MCP_TIMEOUT_SEC`` as a positive float, or
        ``30.0`` when unset/garbled/non-positive. Never raises.
    """
    raw = os.environ.get("TRADINGVIEW_MCP_TIMEOUT_SEC")
    if not raw:
        return _DEFAULT_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Invalid TRADINGVIEW_MCP_TIMEOUT_SEC=%r; using %.1f", raw, _DEFAULT_TIMEOUT)
        return _DEFAULT_TIMEOUT
    return value if value > 0 else _DEFAULT_TIMEOUT


def build_tradingview_client(
    config: TradingViewMcpConfig | None = None,
) -> TradingViewMcpClient | None:
    """Return a configured :class:`TradingViewMcpClient`, or ``None`` when not usable.

    Preconditions: none. ``config`` may be injected (tests); otherwise it is resolved
        from the environment + Unified API store.
    Postconditions: returns a ready client only when the integration is enabled with a
        server URL (:attr:`TradingViewMcpConfig.usable`); returns ``None`` otherwise so
        the market-data service simply omits the TradingView provider from its chain.
        Never raises.
    """
    cfg = config if config is not None else resolve_tradingview_mcp_config()
    if not cfg.usable:
        return None
    return TradingViewMcpClient(
        cfg.server_url,
        auth_token=cfg.auth_token,
        tool_name=cfg.tool_name,
        timeout=_resolve_timeout(),
    )
