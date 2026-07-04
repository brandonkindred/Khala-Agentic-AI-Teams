"""Bridge the TradingView MCP client into the Strategy Lab market-data provider chain."""

from __future__ import annotations

import logging

from shared_env_config import env_float

from .client import TradingViewMcpClient
from .config import TradingViewMcpConfig, resolve_tradingview_mcp_config

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 30.0
# Floor so a garbled/zero/negative value clamps to a usable minimum rather than
# hanging on a 0s timeout — matches how every other numeric knob is parsed.
_MIN_TIMEOUT = 0.001


def _resolve_timeout() -> float:
    """Read the MCP request timeout (seconds) from the environment.

    Postconditions: returns ``TRADINGVIEW_MCP_TIMEOUT_SEC`` as a float via the shared
        :func:`shared_env_config.env_float` parser — ``30.0`` when unset/garbled, and any
        value below ``_MIN_TIMEOUT`` clamped up to it. Never raises.
    """
    return env_float("TRADINGVIEW_MCP_TIMEOUT_SEC", _DEFAULT_TIMEOUT, floor=_MIN_TIMEOUT)


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
