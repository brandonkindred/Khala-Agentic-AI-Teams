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

Stub-builder helpers (``ideation_returning``, ``noop_refine``,
``empty_market_data``, ``failing_sandbox``) are exported so individual
test files don't redefine the same boilerplate. Use them to mock
``orch.ideation_agent.run`` / ``orch.refinement_agent.run`` /
``_fetch_market_data`` / ``run_strategy_code`` with one-line monkeypatch
calls.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

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


# ──────────────────────────────────────────────────────────────────────────
# Stub builders for ``StrategyLabOrchestrator`` collaborators.
# ──────────────────────────────────────────────────────────────────────────


def ideation_returning(
    spec_dict: Dict[str, Any], code: str, *, rationale: str = "scripted rationale"
) -> Callable[..., Tuple[Dict[str, Any], str, str]]:
    """Build a stub ``IdeationAgent.run`` that returns scripted output.

    Tests call ``monkeypatch.setattr(orch.ideation_agent, "run",
    ideation_returning(spec, code))`` to bypass the LLM and pin the spec
    + code the orchestrator's design phase sees.
    """

    def _run(**_kwargs) -> Tuple[Dict[str, Any], str, str]:
        return spec_dict, code, rationale

    return _run


def noop_refine(code: str) -> Callable[..., Tuple[Dict[str, Any], str]]:
    """Build a stub ``RefinementAgent.run`` that returns the same code unchanged.

    Useful when a test forces the refinement loop to exhaust on the same
    failure mode — refinement must produce something or the orchestrator
    would early-exit, but the something must be the unchanged input so the
    loop re-fails the gate it's testing.
    """

    def _run(**_kwargs) -> Tuple[Dict[str, Any], str]:
        return {"changes_made": "no-op"}, code

    return _run


def empty_market_data(
    *, requested: Optional[List[str]] = None, fetched: Optional[List[str]] = None
) -> Callable[..., Any]:
    """Build a stub ``_fetch_market_data`` that returns a "fetched but empty" envelope.

    Pre: ``requested`` defaults to ``["AAPL"]``; ``fetched`` defaults to ``requested``.
    Post: returns a callable suitable for
    ``monkeypatch.setattr(StrategyLabOrchestrator, "_fetch_market_data", ...)``
    that yields a ``_MarketDataFetch`` whose ``data`` maps each requested
    symbol to an empty bar list. This shape matters: the orchestrator
    checks ``if not market_data: break`` (no-market-data short-circuit),
    so the dict must be non-empty even when there are no bars — otherwise
    the test exercises the no-data path instead of the execution-failure
    path it usually intends to.
    """
    requested_symbols = list(requested or ["AAPL"])
    fetched_symbols = list(fetched if fetched is not None else requested_symbols)

    def _fetch(*_a, **_kw):
        from investment_team.strategy_lab.orchestrator import _MarketDataFetch

        return _MarketDataFetch(
            data={s: [] for s in requested_symbols},
            requested_symbols=requested_symbols,
            fetched_symbols=fetched_symbols,
        )

    return _fetch


def failing_sandbox(
    *, error_type: str = "runtime_error", stderr: str = "forced failure for test"
) -> Callable[..., Any]:
    """Build a stub ``run_strategy_code`` that always reports execution failure.

    Use via
    ``monkeypatch.setattr(orchestrator_module, "run_strategy_code", failing_sandbox())``
    when the test wants the synthesis loop to exhaust on the execute path.
    """
    from investment_team.trading_service.modes.sandbox_compat import StrategyRunResult

    def _run(*_a, **_kw) -> StrategyRunResult:
        return StrategyRunResult(
            success=False,
            trades=[],
            stderr=stderr,
            error_type=error_type,
        )

    return _run
