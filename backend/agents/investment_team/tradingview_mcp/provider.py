"""Bridge the TradingView MCP client into the Strategy Lab market-data provider chain."""

from __future__ import annotations

import logging

from shared_env_config import env_float

from .client import TradingViewMcpClient
from .config import TradingViewMcpConfig, resolve_tradingview_mcp_config

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 30.0


def _resolve_timeout() -> float:
    """Read the MCP request timeout (seconds) from the environment.

    Preconditions: none.
    Postconditions: returns ``TRADINGVIEW_MCP_TIMEOUT_SEC`` parsed via the shared
        :func:`shared_env_config.env_float` (so unset/garbled → ``30.0``); a **non-positive**
        value (``0``/negative) also falls back to ``30.0`` rather than becoming a
        near-zero timeout that would make every request time out immediately. Never raises.
    """
    value = env_float("TRADINGVIEW_MCP_TIMEOUT_SEC", _DEFAULT_TIMEOUT)
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
