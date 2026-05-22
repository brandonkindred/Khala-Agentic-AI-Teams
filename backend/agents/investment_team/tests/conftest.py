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

Stub-builder helpers are exported so individual test files don't
redefine the same boilerplate. The split-design pipeline uses
``design_returning``, ``review_returning``, ``code_synthesis_returning``,
and the ``stub_design_loop`` convenience that wires all three at once.
``noop_refine``, ``empty_market_data``, and ``failing_sandbox`` stay
unchanged for the synthesis-loop tests.
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


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
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
        if (
            hasattr(item, "fixturenames")
            and "stub_readiness_market_data_fetch" not in item.fixturenames
        ):  # type: ignore[attr-defined]
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


def design_returning(
    spec_dict: Dict[str, Any], *, rationale: str = "scripted rationale"
) -> Callable[..., Tuple[Dict[str, Any], str]]:
    """Build a stub ``DesignAgent.run`` that returns scripted output.

    Tests call ``monkeypatch.setattr(orch.design_agent, "run",
    design_returning(spec))`` to bypass the LLM and pin the spec the
    orchestrator's design phase sees. The returned tuple matches the
    new agent contract: ``(spec_dict, rationale)`` — no ``strategy_code``.
    """

    def _run(**_kwargs) -> Tuple[Dict[str, Any], str]:
        return dict(spec_dict), rationale

    return _run


def design_revising(
    spec_dicts: List[Dict[str, Any]], *, rationale: str = "scripted revision"
) -> Callable[..., Tuple[Dict[str, Any], str]]:
    """Build a stub ``DesignAgent.revise`` that yields successive specs.

    Each call returns the next entry in ``spec_dicts``; raises
    ``StopIteration`` when exhausted so a test that runs the loop too
    long fails loudly instead of getting a stale spec.
    """
    iterator = iter(spec_dicts)

    def _revise(**_kwargs) -> Tuple[Dict[str, Any], str]:
        return dict(next(iterator)), rationale

    return _revise


def review_returning(*critiques: Any) -> Callable[..., Any]:
    """Build a stub ``DesignReviewAgent.run`` that yields successive critiques.

    Each call returns the next critique in order. When the iterator is
    exhausted the last critique is returned again — handy for "stays
    ready / stays unready" loops that may overrun by one round during
    a test refactor.
    """
    if not critiques:
        raise ValueError("review_returning requires at least one critique")
    queue = list(critiques)

    def _run(*_a, **_kw) -> Any:
        if len(queue) > 1:
            return queue.pop(0)
        return queue[0]

    return _run


def code_synthesis_returning(code: str) -> Callable[..., str]:
    """Build a stub ``CodeSynthesisAgent.run`` that returns scripted code.

    Tests that drive the ``requires_custom_code`` path call
    ``monkeypatch.setattr(orch.code_synthesis_agent, "run",
    code_synthesis_returning(_VALID_CODE))``. The orchestrator passes a
    frozen ``StrategySpec`` in; the stub ignores it.
    """

    def _run(_spec) -> str:
        return code

    return _run


def stub_design_loop(
    monkeypatch: pytest.MonkeyPatch,
    orch: Any,
    spec_dict: Dict[str, Any],
    code: str,
    *,
    rationale: str = "scripted rationale",
) -> None:
    """Wire the full split-design path in one call for synthesis-loop tests.

    Pre: ``orch`` is a constructed ``StrategyLabOrchestrator``.
    Post: ``orch.design_agent.run`` returns ``spec_dict``; review returns
    ``ready=True`` on the first call; ``compile_strategy`` is replaced
    with a stub that returns ``code`` so the test's synthesis loop sees
    the scripted code without depending on the deterministic compiler's
    actual behaviour.
    """
    from investment_team.strategy_lab import orchestrator as orchestrator_module
    from investment_team.strategy_lab.agents.design_review import SpecCritique

    monkeypatch.setattr(orch.design_agent, "run", design_returning(spec_dict, rationale=rationale))

    # ``revise`` is also stubbed so a spec that fails the deterministic
    # readiness gate (e.g. an intentionally-broken test fixture) exhausts
    # the design loop by re-emitting the same bad spec instead of calling
    # the real LLM. Tests that want the design loop to converge wire a
    # spec that *passes* readiness; the review stub then short-circuits
    # on the first round.
    def _revise(*_a, **_kw) -> Tuple[Dict[str, Any], str]:
        return dict(spec_dict), rationale

    monkeypatch.setattr(orch.design_agent, "revise", _revise)
    monkeypatch.setattr(orch.design_review_agent, "run", review_returning(SpecCritique(ready=True)))
    monkeypatch.setattr(orchestrator_module, "compile_strategy", lambda _spec: code)
    # If compile_strategy returns an empty string (test scenario for
    # "ideation produced no code"), the orchestrator falls through to
    # code_synthesis_agent. Stub that too so the test does not depend
    # on the real LLM path.
    monkeypatch.setattr(orch.code_synthesis_agent, "run", code_synthesis_returning(code))
    # The rule-probes gate runs after CodeConformanceGate in the synthesis
    # loop. It executes the compiled strategy against synthetic bars and
    # asserts each rule fires. Pre-existing integration tests that script
    # a fake strategy code (rather than a compiler-produced one) don't
    # carry the order-side semantics or exit-reason strings the probe
    # gate expects, so stub it here to keep the gate's behaviour out of
    # the synthesis-loop pipeline tests. Tests that explicitly exercise
    # the probe gate construct their own ``RuleProbesGate`` instance.
    monkeypatch.setattr(orch.rule_probes_gate, "check", lambda *a, **kw: [])


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
