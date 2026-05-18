"""Shared pytest fixtures for the investment_team test suite.

The ``stub_readiness_market_data_fetch`` fixture is **opt-in**: tests
that build a real ``StrategyLabOrchestrator`` and drive ``run_cycle``
without otherwise mocking the data layer request it explicitly. Making
it autouse would mask the production fail-closed path in every
integration test, defeating the very check ``_readiness_price_provider``
implements (return ``NaN`` when the live service raises / returns no
bars so ``SpecReadinessGate`` Rule 5 surfaces a critical instead of
silently passing).

Tests that test the fail-closed path itself (regression tests in
``test_spec_readiness.py``) must not request this fixture and instead
override ``orch.market_data_service.fetch_ohlcv`` per-instance.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def stub_readiness_market_data_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Return a single synthetic OHLCV bar for any ``fetch_ohlcv`` call.

    Patches the class so every ``MarketDataService`` instance — including
    ones built inside ``StrategyLabOrchestrator.__init__`` — picks up the
    stub before the orchestrator's ``_readiness_price_provider`` reaches
    it. Tests that need a different return can override the instance
    attribute (``orch.market_data_service.fetch_ohlcv = ...``) after the
    fixture has run.
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
