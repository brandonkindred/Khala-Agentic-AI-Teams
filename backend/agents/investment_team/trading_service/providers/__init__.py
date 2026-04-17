"""External market-data provider adapters.

PR 2 introduces a provider registry with free-first defaults (Binance /
Coinbase / Alpaca IEX / OANDA) and paid alternatives that activate when
their API keys are configured (Polygon, Databento, Twelve Data Pro).

The registry is a lazy singleton. Tests build their own via
:class:`ProviderRegistry` directly to avoid globals.
"""

from __future__ import annotations

from functools import lru_cache

from .base import (
    ProviderAdapter,
    ProviderCapabilities,
    ProviderError,
    ProviderRegionBlocked,
)
from .registry import LiveResolution, ProviderRegistry


@lru_cache(maxsize=1)
def default_registry() -> ProviderRegistry:
    """Build (once) and return the process-wide default registry."""
    from . import alpaca, binance, coinbase, databento, oanda, polygon, twelve_data

    reg = ProviderRegistry()

    # ------------------------------------------------------------------
    # Paid providers first — they get evaluated earlier in selection,
    # but only activate when their API key env var is set.
    # ------------------------------------------------------------------
    reg.register(
        polygon.build,
        polygon.CAPABILITIES,
        api_key_env="POLYGON_API_KEY",
    )
    reg.register(
        databento.build,
        databento.CAPABILITIES,
        api_key_env="DATABENTO_API_KEY",
    )
    reg.register(
        twelve_data.build,
        twelve_data.CAPABILITIES,
        api_key_env="TWELVE_DATA_API_KEY",
    )

    # ------------------------------------------------------------------
    # Free defaults, in the order declared in the spec.
    # ------------------------------------------------------------------
    reg.register(
        binance.build,
        binance.CAPABILITIES,
        default_for=["crypto"],
    )
    reg.register(
        coinbase.build,
        coinbase.CAPABILITIES,
        secondary_for=["crypto"],  # Binance geo-block failover
    )
    reg.register(
        alpaca.build,
        alpaca.CAPABILITIES,
        default_for=["equities"],
        api_key_env="ALPACA_API_KEY_ID",  # free but required for auth handshake
    )
    reg.register(
        oanda.build,
        oanda.CAPABILITIES,
        default_for=["fx"],
        api_key_env="OANDA_API_TOKEN",  # free practice token — still required
    )

    return reg


__all__ = [
    "LiveResolution",
    "ProviderAdapter",
    "ProviderCapabilities",
    "ProviderError",
    "ProviderRegionBlocked",
    "ProviderRegistry",
    "default_registry",
]
