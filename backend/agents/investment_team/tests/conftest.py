"""Shared pytest fixtures for the investment_team test suite.

Autouse-stubs the live ``MarketDataService.fetch_ohlcv`` lookup so the
readiness price provider on ``StrategyLabOrchestrator`` sees a
deterministic, finite price during tests. Without this, the production
fail-closed path (``_readiness_price_provider`` returns ``NaN`` when the
live service raises or returns no bars) trips ``SpecReadinessGate``
Rule 5 in every test that builds a real orchestrator and calls
``run_cycle`` — those tests never wanted to exercise the readiness
sizing path and would otherwise short-circuit on a spurious critical.

Tests that explicitly need the fail-closed behaviour (e.g. the
``_readiness_price_provider`` regression tests in
``test_spec_readiness.py``) replace ``orch.market_data_service.fetch_ohlcv``
per-instance after the fixture has run; those instance overrides take
precedence over the class-level stub installed here.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _stub_readiness_market_data_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Return a single synthetic OHLCV bar for any ``fetch_ohlcv`` call.

    Patches the class so every ``MarketDataService`` instance — including
    ones built inside ``StrategyLabOrchestrator.__init__`` — picks up the
    stub before the orchestrator's ``_readiness_price_provider`` reaches
    it. Tests that need a different return can override the instance
    attribute (``orch.market_data_service.fetch_ohlcv = ...``).
    """
    from investment_team.market_data_service import MarketDataService, OHLCVBar

    sentinel = [
        OHLCVBar(
            date="2024-06-01",
            open=100.0,
            high=100.5,
            low=99.5,
            close=100.0,
            volume=1_000_000,
        )
    ]

    def _stub(self, symbol, asset_class, days=365):  # noqa: ARG001 — match signature
        return list(sentinel)

    monkeypatch.setattr(MarketDataService, "fetch_ohlcv", _stub)
