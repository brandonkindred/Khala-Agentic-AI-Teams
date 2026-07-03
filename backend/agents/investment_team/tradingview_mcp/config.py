"""Resolve the TradingView MCP configuration for the Strategy Lab.

Resolution order (first non-empty wins):

1. Environment variables — ``TRADINGVIEW_MCP_URL``, ``TRADINGVIEW_MCP_TOKEN``,
   ``TRADINGVIEW_MCP_TOOL``, ``TRADINGVIEW_MCP_ENABLED``. An explicit operator
   override that works even when the Unified API store is not on the path (isolated
   team containers, CI).
2. The Unified API Integrations store (``/api/integrations/tradingview``), read via a
   best-effort lazy import — the user-facing configuration surface in the mono-process
   deployment. Mirrors ``blogging/shared/medium_integration_access.py``.

The integration is "usable" only when it is **enabled** *and* has a non-empty server
URL; :meth:`TradingViewMcpConfig.usable` encodes that so callers gate on one property.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_TOOL = "get_ohlcv"

_TRUTHY = {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class TradingViewMcpConfig:
    """Immutable snapshot of the TradingView MCP configuration.

    Invariant: ``tool_name`` is always non-empty (defaulted to ``get_ohlcv``).
    """

    enabled: bool
    server_url: str
    auth_token: str
    tool_name: str = _DEFAULT_TOOL

    @property
    def usable(self) -> bool:
        """True iff the integration is enabled and has a server URL to call.

        Postconditions: returns ``True`` only when ``enabled`` and ``server_url`` is
            non-empty — the exact precondition :class:`TradingViewMcpClient` needs.
        """
        return bool(self.enabled and self.server_url)


def _env_flag(name: str) -> bool | None:
    """Parse a truthy/falsey env flag, returning ``None`` when the var is unset.

    Postconditions: ``None`` when ``name`` is absent/blank (so the caller can fall back
        to the store); otherwise ``True`` for ``1/true/yes/on`` (case-insensitive),
        ``False`` for anything else.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    return raw.strip().lower() in _TRUTHY


def _backend_root() -> Path:
    # tradingview_mcp/ -> investment_team/ -> agents/ -> backend/
    return Path(__file__).resolve().parent.parent.parent.parent


def _ensure_backend_on_path() -> None:
    root = str(_backend_root())
    if root not in sys.path:
        sys.path.insert(0, root)


def _config_from_store() -> dict | None:
    """Best-effort read of the Unified API TradingView config.

    Postconditions: returns the store dict, or ``None`` when the Unified API modules
        are not importable (isolated team container) — the caller then relies solely
        on environment variables. Never raises.
    """
    _ensure_backend_on_path()
    try:
        from unified_api.integrations_store import get_tradingview_config
    except Exception as exc:  # noqa: BLE001 - unified_api not on path is an expected fallback
        logger.debug("TradingView integration store unavailable: %s", exc)
        return None
    try:
        return get_tradingview_config()
    except Exception as exc:  # noqa: BLE001 - a store read error must not break data fetching
        logger.warning("TradingView integration config read failed: %s", exc)
        return None


def resolve_tradingview_mcp_config() -> TradingViewMcpConfig:
    """Return the effective TradingView MCP config (env overrides store).

    Preconditions: none.
    Postconditions: returns a :class:`TradingViewMcpConfig`. Each field prefers its
        environment override when set and falls back to the Unified API store value
        (or a safe default). ``enabled`` is ``True`` only when explicitly enabled by
        env flag or store; a missing/garbled value degrades to disabled. Never raises.
    """
    store = _config_from_store() or {}

    env_url = os.environ.get("TRADINGVIEW_MCP_URL", "").strip()
    env_token = os.environ.get("TRADINGVIEW_MCP_TOKEN", "").strip()
    env_tool = os.environ.get("TRADINGVIEW_MCP_TOOL", "").strip()
    env_enabled = _env_flag("TRADINGVIEW_MCP_ENABLED")

    server_url = env_url or str(store.get("mcp_server_url", "")).strip()
    auth_token = env_token or str(store.get("auth_token", "")).strip()
    tool_name = env_tool or str(store.get("tool_name", "")).strip() or _DEFAULT_TOOL
    enabled = env_enabled if env_enabled is not None else bool(store.get("enabled", False))

    return TradingViewMcpConfig(
        enabled=enabled,
        server_url=server_url,
        auth_token=auth_token,
        tool_name=tool_name,
    )
