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
    """Parse a truthy/falsey env flag as a tri-state.

    Preconditions: ``name`` is an environment-variable name.
    Postconditions: ``None`` when ``name`` is absent/blank (so the caller can fall back
        to the store); otherwise the shared :func:`shared_env_config.env_bool` vocabulary
        (``true/1/yes/on`` → ``True``, ``false/0/no/off`` and any unrecognized value →
        ``False``). Reuses the platform helper so the truthy set stays single-sourced.
    """
    from shared_env_config import env_bool

    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    return env_bool(name, False)


def _backend_root() -> Path:
    """Return the ``backend/`` repo root, four parents up from this file.

    Postconditions: returns the path ``tradingview_mcp/ → investment_team/ → agents/ →
        backend/``, so :func:`_ensure_backend_on_path` can put ``unified_api`` on the path.
    """
    return Path(__file__).resolve().parent.parent.parent.parent


def _ensure_backend_on_path() -> None:
    """Ensure the ``backend/`` root is importable so ``unified_api`` can be lazily loaded.

    Postconditions: prepends :func:`_backend_root` to ``sys.path`` when absent; idempotent.
    """
    root = str(_backend_root())
    if root not in sys.path:
        sys.path.insert(0, root)


def _store_accessors():
    """Return the Unified API ``(get_meta, get_token)`` accessors, or ``(None, None)``.

    Preconditions: none.
    Postconditions: returns the split settings/token accessors when ``unified_api`` is
        importable (mono-process deployment); ``(None, None)`` when it is not (isolated
        team container / CI), so the caller relies solely on environment variables. The
        split lets the resolver read the JSON settings without paying the credential-store
        (Postgres) round-trip, and fetch the encrypted token only when the integration is
        actually enabled. Never raises.
    """
    _ensure_backend_on_path()
    try:
        from unified_api.integrations_store import (
            get_tradingview_config_meta,
            get_tradingview_token,
        )
    except Exception as exc:  # noqa: BLE001 - unified_api not on path is an expected fallback
        logger.debug("TradingView integration store unavailable: %s", exc)
        return None, None
    return get_tradingview_config_meta, get_tradingview_token


def resolve_tradingview_mcp_config() -> TradingViewMcpConfig:
    """Return the effective TradingView MCP config (env overrides store).

    Preconditions: none.
    Postconditions: returns a :class:`TradingViewMcpConfig`. Each field prefers its
        environment override and falls back to the Unified API store value (or a safe
        default). The JSON settings are read when the store is available (a cheap local
        file read); the encrypted token is read **only** when the integration resolves to
        enabled+URL — so the common disabled path never pays a credential-store (Postgres)
        round-trip. ``enabled`` is ``True`` only when explicitly enabled by env flag or
        store; a missing/garbled value degrades to disabled. Never raises.
    """
    env_url = os.environ.get("TRADINGVIEW_MCP_URL", "").strip()
    env_token = os.environ.get("TRADINGVIEW_MCP_TOKEN", "").strip()
    env_tool = os.environ.get("TRADINGVIEW_MCP_TOOL", "").strip()
    env_enabled = _env_flag("TRADINGVIEW_MCP_ENABLED")

    get_meta, get_token = _store_accessors()

    # Read the JSON settings whenever the store is available — it's a cheap local file
    # read, and env values still take precedence field-by-field below. (The expensive
    # part, the encrypted-token credential-store read, stays gated on enabled+URL.)
    meta: dict = {}
    if get_meta is not None:
        try:
            meta = get_meta() or {}
        except Exception as exc:  # noqa: BLE001 - a store read must not break data fetching
            logger.warning("TradingView settings read failed: %s", exc)

    server_url = env_url or str(meta.get("mcp_server_url", "")).strip()
    tool_name = env_tool or str(meta.get("tool_name", "")).strip() or _DEFAULT_TOOL
    enabled = env_enabled if env_enabled is not None else bool(meta.get("enabled", False))

    # Only pay the credential-store read when the integration is actually usable.
    auth_token = env_token
    if not auth_token and enabled and server_url and get_token is not None:
        try:
            auth_token = (get_token() or "").strip()
        except Exception as exc:  # noqa: BLE001 - a token read must not break data fetching
            logger.warning("TradingView token read failed: %s", exc)

    return TradingViewMcpConfig(
        enabled=enabled,
        server_url=server_url,
        auth_token=auth_token,
        tool_name=tool_name,
    )
