"""Unit tests for the provider registry selection logic.

Covers the three-tier selection order from §3.2 plus the crypto
Binance → Coinbase geo-failover from the Binance-blocked-region table.
"""

from __future__ import annotations

import os
from typing import Iterator, Optional

import pytest

from investment_team.trading_service.data_stream.protocol import BarEvent
from investment_team.trading_service.data_stream.resampler import NativeEvent
from investment_team.trading_service.providers.base import (
    ProviderCapabilities,
    ProviderRegionBlocked,
)
from investment_team.trading_service.providers.registry import ProviderRegistry


class _StubAdapter:
    """Minimal in-memory adapter used by the tests."""

    def __init__(
        self,
        name: str,
        *,
        supports: set[str],
        is_paid: bool = False,
        historical: bool = True,
        live: bool = True,
        region_block: bool = False,
    ) -> None:
        self.capabilities = ProviderCapabilities(
            name=name,
            supports=supports,
            is_paid=is_paid,
            historical_timeframes={"1m"} if historical else set(),
            live_timeframes={"1m"} if live else set(),
        )
        self._region_block = region_block

    def smallest_available(self, asset_class: str, *, live: bool) -> Optional[str]:
        return "1m"

    def historical(self, **kwargs) -> Iterator[BarEvent]:
        return iter(())

    def live(self, **kwargs) -> Iterator[NativeEvent]:
        if self._region_block:
            raise ProviderRegionBlocked(f"{self.capabilities.name} region-blocked")
        return iter(())


# ---------------------------------------------------------------------------
# Selection precedence
# ---------------------------------------------------------------------------


def _registry_with_defaults() -> ProviderRegistry:
    reg = ProviderRegistry()
    reg.register(
        lambda: _StubAdapter("binance", supports={"crypto"}),
        ProviderCapabilities(
            name="binance",
            supports={"crypto"},
            historical_timeframes={"1m"},
            live_timeframes={"1m"},
        ),
        default_for=["crypto"],
    )
    reg.register(
        lambda: _StubAdapter("coinbase", supports={"crypto"}),
        ProviderCapabilities(
            name="coinbase",
            supports={"crypto"},
            historical_timeframes={"1m"},
            live_timeframes={"1m"},
        ),
        secondary_for=["crypto"],
    )
    reg.register(
        lambda: _StubAdapter("polygon", supports={"crypto", "equities"}, is_paid=True),
        ProviderCapabilities(
            name="polygon",
            supports={"crypto", "equities"},
            is_paid=True,
            historical_timeframes={"1m"},
            live_timeframes={"1m"},
        ),
        api_key_env="POLYGON_API_KEY_TEST",
    )
    return reg


def test_free_default_selected_when_no_paid_key() -> None:
    reg = _registry_with_defaults()
    os.environ.pop("POLYGON_API_KEY_TEST", None)
    adapter = reg.resolve(asset_class="crypto", direction="live")
    assert adapter.capabilities.name == "binance"


def test_paid_provider_preferred_when_key_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    reg = _registry_with_defaults()
    monkeypatch.setenv("POLYGON_API_KEY_TEST", "sekret")
    adapter = reg.resolve(asset_class="crypto", direction="live")
    assert adapter.capabilities.name == "polygon"


def test_explicit_override_wins_over_paid_and_free(monkeypatch: pytest.MonkeyPatch) -> None:
    reg = _registry_with_defaults()
    monkeypatch.setenv("POLYGON_API_KEY_TEST", "sekret")
    adapter = reg.resolve(asset_class="crypto", direction="live", explicit="binance")
    assert adapter.capabilities.name == "binance"


def test_explicit_unknown_provider_raises() -> None:
    reg = _registry_with_defaults()
    with pytest.raises(KeyError, match="nope"):
        reg.resolve(asset_class="crypto", direction="live", explicit="nope")


def test_explicit_provider_without_asset_support_raises() -> None:
    reg = _registry_with_defaults()
    # binance only supports crypto, not equities
    with pytest.raises(ValueError, match="does not support asset_class"):
        reg.resolve(asset_class="equities", direction="live", explicit="binance")


def test_direction_filter_excludes_historical_only_adapters() -> None:
    reg = ProviderRegistry()
    reg.register(
        lambda: _StubAdapter("hist_only", supports={"crypto"}, live=False),
        ProviderCapabilities(
            name="hist_only",
            supports={"crypto"},
            historical_timeframes={"1m"},
            live_timeframes=set(),
        ),
        default_for=["crypto"],
    )
    with pytest.raises(LookupError, match="no provider available"):
        reg.resolve(asset_class="crypto", direction="live")


def test_no_provider_available_raises() -> None:
    reg = ProviderRegistry()
    with pytest.raises(LookupError, match="no provider available"):
        reg.resolve(asset_class="crypto", direction="live")


# ---------------------------------------------------------------------------
# Geo-failover
# ---------------------------------------------------------------------------


def test_resolve_live_returns_fallback_for_crypto_when_no_explicit() -> None:
    reg = _registry_with_defaults()
    resolution = reg.resolve_live(asset_class="crypto")
    assert resolution.primary_name == "binance"
    assert resolution.fallback_name == "coinbase"


def test_resolve_live_no_fallback_when_user_pins_provider() -> None:
    reg = _registry_with_defaults()
    resolution = reg.resolve_live(asset_class="crypto", explicit="binance")
    assert resolution.primary_name == "binance"
    # An explicit pin means the user opted out of auto-failover.
    assert resolution.fallback is None


def test_resolve_live_paid_primary_no_crypto_secondary(monkeypatch: pytest.MonkeyPatch) -> None:
    reg = _registry_with_defaults()
    monkeypatch.setenv("POLYGON_API_KEY_TEST", "sekret")
    resolution = reg.resolve_live(asset_class="crypto")
    assert resolution.primary_name == "polygon"
    # Coinbase is still the secondary_for crypto — failover still applies even
    # when the primary is paid (covers "paid API key is flaky / regional").
    assert resolution.fallback_name == "coinbase"


# ---------------------------------------------------------------------------
# describe_all
# ---------------------------------------------------------------------------


def test_describe_all_reports_has_key(monkeypatch: pytest.MonkeyPatch) -> None:
    reg = _registry_with_defaults()
    monkeypatch.delenv("POLYGON_API_KEY_TEST", raising=False)
    listing = reg.describe_all()
    polygon_row = next(r for r in listing if r["name"] == "polygon")
    assert polygon_row["has_key"] is False
    assert polygon_row["is_paid"] is True

    monkeypatch.setenv("POLYGON_API_KEY_TEST", "sekret")
    listing = reg.describe_all()
    polygon_row = next(r for r in listing if r["name"] == "polygon")
    assert polygon_row["has_key"] is True


# ---------------------------------------------------------------------------
# Env-var overrides (INVESTMENT_{LIVE,HISTORICAL}_PROVIDER_*)
# ---------------------------------------------------------------------------


def test_env_live_override_forces_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Operator env override must beat the free default."""
    reg = _registry_with_defaults()
    monkeypatch.setenv("INVESTMENT_LIVE_PROVIDER_CRYPTO", "coinbase")
    adapter = reg.resolve(asset_class="crypto", direction="live")
    assert adapter.capabilities.name == "coinbase"


def test_env_live_override_beats_paid_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env override outranks paid-with-key; only request-level explicit beats it."""
    reg = _registry_with_defaults()
    monkeypatch.setenv("POLYGON_API_KEY_TEST", "sekret")
    monkeypatch.setenv("INVESTMENT_LIVE_PROVIDER_CRYPTO", "binance")
    adapter = reg.resolve(asset_class="crypto", direction="live")
    assert adapter.capabilities.name == "binance"


def test_explicit_request_pin_beats_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    reg = _registry_with_defaults()
    monkeypatch.setenv("INVESTMENT_LIVE_PROVIDER_CRYPTO", "coinbase")
    adapter = reg.resolve(asset_class="crypto", direction="live", explicit="binance")
    assert adapter.capabilities.name == "binance"


def test_historical_env_override_uses_separate_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """Historical and live env vars are independent knobs."""
    reg = _registry_with_defaults()
    # Set only LIVE; historical should still hit the free default.
    monkeypatch.setenv("INVESTMENT_LIVE_PROVIDER_CRYPTO", "coinbase")
    monkeypatch.delenv("INVESTMENT_HISTORICAL_PROVIDER_CRYPTO", raising=False)
    live_adapter = reg.resolve(asset_class="crypto", direction="live")
    hist_adapter = reg.resolve(asset_class="crypto", direction="historical")
    assert live_adapter.capabilities.name == "coinbase"
    assert hist_adapter.capabilities.name == "binance"


def test_invalid_env_override_falls_back_silently(monkeypatch: pytest.MonkeyPatch) -> None:
    """A misconfigured env var must not wedge the server."""
    reg = _registry_with_defaults()
    monkeypatch.setenv("INVESTMENT_LIVE_PROVIDER_CRYPTO", "nonexistent_provider")
    adapter = reg.resolve(asset_class="crypto", direction="live")
    # Falls through to paid-with-key (none set) → free default.
    assert adapter.capabilities.name == "binance"


def test_env_override_for_unsupported_asset_class_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the pinned provider doesn't support the class, fall back cleanly."""
    reg = _registry_with_defaults()
    # binance only supports crypto; asking for equities with a binance pin
    # should ignore the pin and fall back through the selection chain. In
    # this registry there's no equities provider, so LookupError is expected.
    monkeypatch.setenv("INVESTMENT_LIVE_PROVIDER_EQUITIES", "binance")
    with pytest.raises(LookupError):
        reg.resolve(asset_class="equities", direction="live")


def test_empty_env_var_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    reg = _registry_with_defaults()
    monkeypatch.setenv("INVESTMENT_LIVE_PROVIDER_CRYPTO", "   ")
    adapter = reg.resolve(asset_class="crypto", direction="live")
    assert adapter.capabilities.name == "binance"


# ---------------------------------------------------------------------------
# Asset-class normalization (Codex P1 on PR #188)
# ---------------------------------------------------------------------------


def test_stocks_alias_routes_to_equities_default() -> None:
    """Strategy Lab records use "stocks"; registry resolves on "equities"."""
    reg = ProviderRegistry()
    reg.register(
        lambda: _StubAdapter("alpaca", supports={"equities"}),
        ProviderCapabilities(
            name="alpaca",
            supports={"equities"},
            historical_timeframes={"1m"},
            live_timeframes={"1m"},
        ),
        default_for=["equities"],
    )
    adapter = reg.resolve(asset_class="stocks", direction="live")
    assert adapter.capabilities.name == "alpaca"


def test_forex_alias_routes_to_fx_default() -> None:
    reg = ProviderRegistry()
    reg.register(
        lambda: _StubAdapter("oanda", supports={"fx"}),
        ProviderCapabilities(
            name="oanda",
            supports={"fx"},
            historical_timeframes={"1m"},
            live_timeframes={"1m"},
        ),
        default_for=["fx"],
    )
    adapter = reg.resolve(asset_class="forex", direction="live")
    assert adapter.capabilities.name == "oanda"


def test_resolve_live_normalizes_asset_class_for_fallback() -> None:
    """Geo-failover secondary lookup must match the normalized label."""
    reg = ProviderRegistry()
    reg.register(
        lambda: _StubAdapter("binance", supports={"crypto"}),
        ProviderCapabilities(
            name="binance",
            supports={"crypto"},
            historical_timeframes={"1m"},
            live_timeframes={"1m"},
        ),
        default_for=["crypto"],
    )
    reg.register(
        lambda: _StubAdapter("coinbase", supports={"crypto"}),
        ProviderCapabilities(
            name="coinbase",
            supports={"crypto"},
            historical_timeframes={"1m"},
            live_timeframes={"1m"},
        ),
        secondary_for=["crypto"],
    )
    # Even though "cryptocurrency" isn't the registered key, it should
    # resolve to both primary and secondary.
    resolution = reg.resolve_live(asset_class="cryptocurrency")
    assert resolution.primary_name == "binance"
    assert resolution.fallback_name == "coinbase"


# ---------------------------------------------------------------------------
# Paid-provider implementation-readiness (Codex P1 on PR #188)
# ---------------------------------------------------------------------------


def _registry_with_unimplemented_paid() -> ProviderRegistry:
    """Build a registry where the paid provider is registered but not wired."""
    reg = ProviderRegistry()
    reg.register(
        lambda: _StubAdapter("binance", supports={"crypto"}),
        ProviderCapabilities(
            name="binance",
            supports={"crypto"},
            historical_timeframes={"1m"},
            live_timeframes={"1m"},
        ),
        default_for=["crypto"],
    )
    reg.register(
        lambda: _StubAdapter("polygon", supports={"crypto", "equities"}, is_paid=True),
        ProviderCapabilities(
            name="polygon",
            supports={"crypto", "equities"},
            is_paid=True,
            historical_timeframes={"1m"},
            live_timeframes={"1m"},
            implemented=False,  # ← the guard under test
        ),
        api_key_env="POLYGON_API_KEY_TEST",
    )
    return reg


def test_unimplemented_paid_provider_not_auto_selected_on_key_presence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Key presence alone must NOT route past the working free default.

    Real-world scenario: a user sets POLYGON_API_KEY for an unrelated tool;
    without this guard the registry would select Polygon even though its
    pumps are stubbed, and paper trading would fail at runtime with
    NotImplementedError.
    """
    reg = _registry_with_unimplemented_paid()
    monkeypatch.setenv("POLYGON_API_KEY_TEST", "sekret")
    adapter = reg.resolve(asset_class="crypto", direction="live")
    # Must fall through to the implemented free default.
    assert adapter.capabilities.name == "binance"


def test_implemented_paid_provider_still_preferred_when_key_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression baseline: once the paid pump is wired (implemented=True),
    the key-presence preference rule still fires — no behaviour change for
    the happy path.
    """
    reg = ProviderRegistry()
    reg.register(
        lambda: _StubAdapter("binance", supports={"crypto"}),
        ProviderCapabilities(
            name="binance",
            supports={"crypto"},
            historical_timeframes={"1m"},
            live_timeframes={"1m"},
        ),
        default_for=["crypto"],
    )
    reg.register(
        lambda: _StubAdapter("polygon", supports={"crypto"}, is_paid=True),
        ProviderCapabilities(
            name="polygon",
            supports={"crypto"},
            is_paid=True,
            historical_timeframes={"1m"},
            live_timeframes={"1m"},
            implemented=True,
        ),
        api_key_env="POLYGON_API_KEY_TEST",
    )
    monkeypatch.setenv("POLYGON_API_KEY_TEST", "sekret")
    adapter = reg.resolve(asset_class="crypto", direction="live")
    assert adapter.capabilities.name == "polygon"


def test_explicit_pin_still_resolves_unimplemented_provider() -> None:
    """Users who explicitly opt into a stub adapter get the stub (so the
    opt-in is visible via NotImplementedError at stream time), not a silent
    fallback. Only *auto-selection* skips unimplemented adapters."""
    reg = _registry_with_unimplemented_paid()
    adapter = reg.resolve(asset_class="crypto", direction="live", explicit="polygon")
    assert adapter.capabilities.name == "polygon"


def test_describe_all_reports_implemented_field() -> None:
    reg = _registry_with_unimplemented_paid()
    listing = reg.describe_all()
    binance_row = next(r for r in listing if r["name"] == "binance")
    polygon_row = next(r for r in listing if r["name"] == "polygon")
    assert binance_row["implemented"] is True
    assert polygon_row["implemented"] is False


def test_registering_duplicate_name_raises() -> None:
    reg = _registry_with_defaults()
    with pytest.raises(ValueError, match="already registered"):
        reg.register(
            lambda: _StubAdapter("binance", supports={"crypto"}),
            ProviderCapabilities(
                name="binance",
                supports={"crypto"},
                historical_timeframes={"1m"},
                live_timeframes={"1m"},
            ),
        )
