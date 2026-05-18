"""Shared pytest fixtures + markers for the investment_team test suite.

Tests that build a real ``StrategyLabOrchestrator`` and drive
``run_cycle`` need a deterministic ``MarketDataService.fetch_ohlcv``
because ``_readiness_price_provider`` fails closed (returns ``NaN``)
when the live data layer returns nothing — which would otherwise trip
``SpecReadinessGate`` Rule 5 on every integration test, masking the
real failure being tested.

The opt-in mechanism is a **marker**:

    @pytest.mark.strategy_lab_integration
    def test_run_cycle_does_X(...): ...

or at module scope:

    pytestmark = pytest.mark.strategy_lab_integration

Marked tests automatically receive the ``stub_readiness_market_data_fetch``
fixture via ``pytest_collection_modifyitems``. Tests that exercise the
fail-closed path itself (regression tests in ``test_spec_readiness.py``)
must NOT carry the marker; they override
``orch.market_data_service.fetch_ohlcv`` per-instance instead.

The marker name documents test intent (this is an integration test
against the Strategy Lab pipeline) rather than the implementation
detail (which fixture to request) — easier to remember for future
tests, and harder to forget than a manually-typed fixture name.
"""

from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "strategy_lab_integration: test drives a real StrategyLabOrchestrator "
        "through run_cycle; auto-applies stub_readiness_market_data_fetch.",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Auto-apply ``stub_readiness_market_data_fetch`` to every
    ``strategy_lab_integration`` test.

    Pre: every test that needs the readiness stub carries the marker
    (at item or module scope).
    Post: items carrying the marker pick up the fixture via fixturenames,
    so the live readiness path is exercised everywhere else.
    """
    for item in items:
        if item.get_closest_marker("strategy_lab_integration") is None:
            continue
        # pytest fixtures are requested by name. Appending to ``fixturenames``
        # only works on Function items; non-function items don't have the slot.
        if hasattr(item, "fixturenames") and "stub_readiness_market_data_fetch" not in item.fixturenames:  # type: ignore[attr-defined]
            item.fixturenames.append("stub_readiness_market_data_fetch")  # type: ignore[attr-defined]


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
