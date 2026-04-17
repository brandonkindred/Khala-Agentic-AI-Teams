"""Provider registry — free-first selection with paid overrides.

Selection order for a given ``(asset_class, direction)``:

1. **Explicit override** (request-level ``provider_id`` or env-var
   ``INVESTMENT_LIVE_PROVIDER_*`` / ``INVESTMENT_HISTORICAL_PROVIDER_*``).
2. **Paid provider with an API key configured**, ranked by registration order.
3. **Free default** for that asset class.

The Binance → Coinbase geo-failover is handled at session open by
:func:`resolve_live` (see ``system_design/pr2_live_data_and_paper_cutover.md``
§3.2 "Binance → Coinbase geo-failover").
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .base import ProviderAdapter, ProviderCapabilities, ProviderRegionBlocked

logger = logging.getLogger(__name__)


AdapterFactory = Callable[[], ProviderAdapter]


@dataclass
class _Registration:
    factory: AdapterFactory
    capabilities: ProviderCapabilities
    #: Asset classes for which this adapter is the **free default** (if any).
    default_for: List[str] = field(default_factory=list)
    #: Secondary free default — used only when the primary adapter for the
    #: same asset class raises :class:`ProviderRegionBlocked`. One secondary
    #: per asset class at most; populated for Coinbase on ``crypto``.
    secondary_for: List[str] = field(default_factory=list)
    #: Name of env var whose presence indicates the user's configuration
    #: enables this adapter's paid features (only meaningful when
    #: ``capabilities.is_paid`` is True).
    api_key_env: Optional[str] = None


class ProviderRegistry:
    """In-memory registry of provider adapters.

    The default registry is constructed via :func:`build_default_registry`.
    Tests construct their own registry with stub adapters.
    """

    def __init__(self) -> None:
        self._registrations: Dict[str, _Registration] = {}
        # Insertion order is preserved by dict in 3.7+; we rely on that for
        # deterministic paid-provider ranking.

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        factory: AdapterFactory,
        capabilities: ProviderCapabilities,
        *,
        default_for: Optional[List[str]] = None,
        secondary_for: Optional[List[str]] = None,
        api_key_env: Optional[str] = None,
    ) -> None:
        if capabilities.name in self._registrations:
            raise ValueError(f"provider {capabilities.name!r} already registered")
        self._registrations[capabilities.name] = _Registration(
            factory=factory,
            capabilities=capabilities,
            default_for=list(default_for or []),
            secondary_for=list(secondary_for or []),
            api_key_env=api_key_env,
        )

    def names(self) -> List[str]:
        return list(self._registrations.keys())

    def get(self, name: str) -> ProviderAdapter:
        if name not in self._registrations:
            raise KeyError(f"provider {name!r} is not registered")
        return self._registrations[name].factory()

    def describe_all(self) -> List[Dict[str, object]]:
        """Return a serializable snapshot for the ``GET /providers`` endpoint."""
        out: List[Dict[str, object]] = []
        for reg in self._registrations.values():
            has_key = bool(reg.api_key_env and os.environ.get(reg.api_key_env))
            out.append(
                {
                    "name": reg.capabilities.name,
                    "supports": sorted(reg.capabilities.supports),
                    "is_paid": reg.capabilities.is_paid,
                    "has_key": has_key,
                    "is_default_for": list(reg.default_for),
                    "historical_timeframes": sorted(reg.capabilities.historical_timeframes),
                    "live_timeframes": sorted(reg.capabilities.live_timeframes),
                }
            )
        return out

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    def resolve(
        self,
        *,
        asset_class: str,
        direction: str,
        explicit: Optional[str] = None,
    ) -> ProviderAdapter:
        """Return the adapter chosen for ``(asset_class, direction)``.

        ``direction`` is ``"historical"`` or ``"live"``. ``explicit`` wins
        unconditionally if given; else paid providers with a configured key;
        else the free default. Raises :class:`LookupError` if nothing matches.
        """
        if direction not in {"historical", "live"}:
            raise ValueError("direction must be 'historical' or 'live'")
        reg = self._pick(asset_class=asset_class, direction=direction, explicit=explicit)
        if reg is None:
            raise LookupError(
                f"no provider available for asset_class={asset_class!r} direction={direction!r}"
            )
        return reg.factory()

    def resolve_live(
        self,
        *,
        asset_class: str,
        explicit: Optional[str] = None,
    ) -> "LiveResolution":
        """Like :meth:`resolve` but with geo-failover support for crypto.

        Returns both the **chosen** adapter and a fallback adapter (if any)
        that the caller should try if the chosen one raises
        :class:`ProviderRegionBlocked` *before the first bar is emitted*.
        Only applies when ``direction="live"``.
        """
        primary_reg = self._pick(asset_class=asset_class, direction="live", explicit=explicit)
        if primary_reg is None:
            raise LookupError(
                f"no provider available for asset_class={asset_class!r} direction='live'"
            )

        fallback_reg: Optional[_Registration] = None
        if explicit is None:
            # Only auto-failover when the user hasn't pinned a provider.
            for reg in self._registrations.values():
                if reg is primary_reg:
                    continue
                if asset_class in reg.secondary_for:
                    fallback_reg = reg
                    break

        return LiveResolution(
            primary=primary_reg.factory(),
            primary_name=primary_reg.capabilities.name,
            fallback=fallback_reg.factory() if fallback_reg else None,
            fallback_name=fallback_reg.capabilities.name if fallback_reg else None,
        )

    # ------------------------------------------------------------------

    def _pick(
        self,
        *,
        asset_class: str,
        direction: str,
        explicit: Optional[str],
    ) -> Optional[_Registration]:
        # 1. Explicit (request-level) wins.
        if explicit is not None:
            return self._resolve_pinned(explicit, asset_class=asset_class, direction=direction)

        # 2. Env-var override (operator-level). Documented as
        #    INVESTMENT_LIVE_PROVIDER_{CRYPTO,EQUITIES,FX} for live streams and
        #    INVESTMENT_HISTORICAL_PROVIDER_{...} for historical.
        env_pinned = _env_override_for(asset_class=asset_class, direction=direction)
        if env_pinned is not None:
            # A misconfigured env var shouldn't wedge the server — log and fall
            # through to paid/default selection if the pinned provider isn't
            # usable.
            try:
                return self._resolve_pinned(
                    env_pinned, asset_class=asset_class, direction=direction
                )
            except (KeyError, ValueError) as exc:
                logger.warning(
                    "env override %s=%r ignored: %s",
                    _env_var_name(asset_class=asset_class, direction=direction),
                    env_pinned,
                    exc,
                )

        # 3. Paid provider with API key configured.
        for reg in self._registrations.values():
            if not reg.capabilities.is_paid:
                continue
            if asset_class not in reg.capabilities.supports:
                continue
            if not _has_direction(reg, direction):
                continue
            if reg.api_key_env and os.environ.get(reg.api_key_env):
                logger.debug(
                    "registry selecting paid provider %s for %s/%s",
                    reg.capabilities.name,
                    asset_class,
                    direction,
                )
                return reg

        # 4. Free default for this asset class.
        for reg in self._registrations.values():
            if asset_class in reg.default_for and _has_direction(reg, direction):
                return reg

        # 5. Any free provider that supports it (last-resort).
        for reg in self._registrations.values():
            if reg.capabilities.is_paid:
                continue
            if asset_class in reg.capabilities.supports and _has_direction(reg, direction):
                return reg

        return None

    def _resolve_pinned(
        self,
        name: str,
        *,
        asset_class: str,
        direction: str,
    ) -> _Registration:
        """Shared pin-resolution used by explicit and env-override paths."""
        if name not in self._registrations:
            raise KeyError(f"provider {name!r} is not registered")
        reg = self._registrations[name]
        if asset_class not in reg.capabilities.supports:
            raise ValueError(f"provider {name!r} does not support asset_class={asset_class!r}")
        if not _has_direction(reg, direction):
            raise ValueError(f"provider {name!r} does not support direction={direction!r}")
        return reg


def _env_var_name(*, asset_class: str, direction: str) -> str:
    """Return the env-var name that overrides selection for this combination."""
    prefix = (
        "INVESTMENT_LIVE_PROVIDER_" if direction == "live" else "INVESTMENT_HISTORICAL_PROVIDER_"
    )
    return f"{prefix}{asset_class.upper()}"


def _env_override_for(*, asset_class: str, direction: str) -> Optional[str]:
    """Read the ``INVESTMENT_{LIVE,HISTORICAL}_PROVIDER_*`` env var, if set."""
    raw = os.environ.get(_env_var_name(asset_class=asset_class, direction=direction))
    if raw is None:
        return None
    value = raw.strip()
    return value or None


def _has_direction(reg: _Registration, direction: str) -> bool:
    if direction == "historical":
        return bool(reg.capabilities.historical_timeframes)
    return bool(reg.capabilities.live_timeframes)


@dataclass
class LiveResolution:
    """Result of :meth:`ProviderRegistry.resolve_live`."""

    primary: ProviderAdapter
    primary_name: str
    fallback: Optional[ProviderAdapter] = None
    fallback_name: Optional[str] = None


__all__ = [
    "AdapterFactory",
    "LiveResolution",
    "ProviderRegistry",
    "ProviderRegionBlocked",
]
