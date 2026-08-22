"""Direct unit coverage for the design-review-rounds helpers extracted out of
:meth:`StrategyLabOrchestrator._run_design_review_rounds`.

The round loop was decomposed into named helpers:

- :meth:`_validate_and_memoize_readiness` — the deterministic readiness gate,
  memoized on the spec signature (re-validates only when the spec changed).
- :meth:`_run_mechanical_repair_stages` — the two-stage mechanical pre-flight.
- :meth:`_review_and_handle_critique` — reviewer vs. synthetic readiness critique.
- :meth:`_revise_with_regression_notice` — designer revision with a regression
  notice.

The mechanical-repair / review / revise helpers are exercised end-to-end by the
existing ``test_strategy_lab_design_loop.py`` / ``_mechanical_repair.py`` /
``_critique_ledger.py`` suites; this file pins the readiness-memoization
contract (the issue's named win) and the revise contract in isolation.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List

from investment_team.models import BacktestConfig, StrategySpec
from investment_team.strategy_lab.agents.design_review import SpecCritique
from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator
from investment_team.strategy_lab.quality_gates.models import QualityGateResult
from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    IndicatorRef,
    Predicate,
    SignalExitRule,
)


def _config() -> BacktestConfig:
    return BacktestConfig(
        start_date="2023-01-01",
        end_date="2023-12-31",
        initial_capital=100_000.0,
    )


def _spec() -> StrategySpec:
    return StrategySpec(
        strategy_id="design-review-helper-test",
        authored_by="test",
        asset_class="stocks",
        hypothesis="RSI mean reversion",
        signal_definition="RSI(14) crossings",
        timeframe="1d",
        entry_rules=[
            EntryRule(
                side="long",
                when=Predicate(lhs=IndicatorRef(name="rsi", params={"period": 14}), op="<", rhs=30),
            )
        ],
        exit_rules=[
            SignalExitRule(
                when=Predicate(lhs=IndicatorRef(name="rsi", params={"period": 14}), op=">", rhs=70)
            )
        ],
        risk_limits={"max_position_pct": 5, "max_drawdown_pct": 10},
        speculative=False,
        target_symbols=["QQQ"],
    )


def _spec_dict() -> Dict[str, Any]:
    return {
        "asset_class": "stocks",
        "hypothesis": "RSI mean reversion on a small universe",
        "signal_definition": "RSI(14) crossings",
        "timeframe": "1d",
        "entry_rules": [
            EntryRule(
                side="long",
                when=Predicate(lhs=IndicatorRef(name="rsi", params={"period": 14}), op="<", rhs=30),
            ).model_dump()
        ],
        "exit_rules": [
            SignalExitRule(
                when=Predicate(lhs=IndicatorRef(name="rsi", params={"period": 14}), op=">", rhs=70)
            ).model_dump()
        ],
        "risk_limits": {"max_position_pct": 5, "max_drawdown_pct": 10},
        "target_symbols": ["QQQ"],
        "speculative": False,
    }


def _critical() -> QualityGateResult:
    return QualityGateResult(
        gate_name="spec_readiness",
        passed=False,
        severity="critical",
        phase="design",
        details="missing period",
    )


# ---------------------------------------------------------------------------
# _validate_and_memoize_readiness
# ---------------------------------------------------------------------------


def test_readiness_validates_and_reports_ready(monkeypatch) -> None:
    """First call (no prior signature) validates and, with no critical, reports
    ready."""
    orch = StrategyLabOrchestrator()
    calls: List[int] = []
    monkeypatch.setattr(
        orch.spec_readiness_gate,
        "validate",
        lambda spec, phase, backtest_config: calls.append(1) or [],
    )
    gates: List[QualityGateResult] = []

    results, signature, ready = orch._validate_and_memoize_readiness(
        spec=_spec(),
        config=_config(),
        last_readiness_signature=None,
        readiness_results=[],
        all_gate_results=gates,
    )

    assert calls == [1]
    assert ready is True
    assert signature is not None
    assert results == []


def test_readiness_memoized_skips_revalidation(monkeypatch) -> None:
    """A second call with the same spec + memoized signature does not
    re-validate — it returns the cached verdict."""
    orch = StrategyLabOrchestrator()
    calls: List[int] = []
    monkeypatch.setattr(
        orch.spec_readiness_gate,
        "validate",
        lambda spec, phase, backtest_config: calls.append(1) or [],
    )
    spec = _spec()
    gates: List[QualityGateResult] = []

    results, signature, _ready = orch._validate_and_memoize_readiness(
        spec=spec,
        config=_config(),
        last_readiness_signature=None,
        readiness_results=[],
        all_gate_results=gates,
    )
    results2, signature2, ready2 = orch._validate_and_memoize_readiness(
        spec=spec,
        config=_config(),
        last_readiness_signature=signature,
        readiness_results=results,
        all_gate_results=gates,
    )

    assert calls == [1], "unchanged signature must not re-validate"
    assert signature2 == signature
    assert ready2 is True


def test_readiness_revalidates_on_stale_signature(monkeypatch) -> None:
    """A mismatched prior signature forces a re-validation."""
    orch = StrategyLabOrchestrator()
    calls: List[int] = []
    monkeypatch.setattr(
        orch.spec_readiness_gate,
        "validate",
        lambda spec, phase, backtest_config: calls.append(1) or [],
    )

    orch._validate_and_memoize_readiness(
        spec=_spec(),
        config=_config(),
        last_readiness_signature=("stale", "signature"),
        readiness_results=[],
        all_gate_results=[],
    )

    assert calls == [1]


def test_readiness_reports_not_ready_on_critical(monkeypatch) -> None:
    """A readiness critical yields ``deterministic_ready=False``."""
    orch = StrategyLabOrchestrator()
    monkeypatch.setattr(
        orch.spec_readiness_gate,
        "validate",
        lambda spec, phase, backtest_config: [_critical()],
    )

    _results, _signature, ready = orch._validate_and_memoize_readiness(
        spec=_spec(),
        config=_config(),
        last_readiness_signature=None,
        readiness_results=[],
        all_gate_results=[],
    )

    assert ready is False


# ---------------------------------------------------------------------------
# _revise_with_regression_notice
# ---------------------------------------------------------------------------


def test_revise_returns_rebuilt_spec_and_new_rationale(monkeypatch) -> None:
    """The revise helper rebuilds the spec from the designer payload and returns
    the new rationale, carrying the supplied ``strategy_id``."""
    orch = StrategyLabOrchestrator()
    monkeypatch.setattr(
        orch.design_agent,
        "revise",
        lambda *a, **k: (_spec_dict(), "new rationale"),
    )
    critique = SpecCritique(ready=False, rationale="fix the entry")
    delta = SimpleNamespace(regressed=[])

    spec_out, rationale_out = orch._revise_with_regression_notice(
        spec=_spec(),
        rationale="old rationale",
        critique=critique,
        delta=delta,
        critique_history=[critique],
        strategy_id="rebuilt-id",
        mechanical_repair_count=0,
        drift_collector=None,
    )

    assert rationale_out == "new rationale"
    assert spec_out.strategy_id == "rebuilt-id"


def test_revise_forwards_skip_self_review_to_design_agent(monkeypatch) -> None:
    """``skip_self_review`` is forwarded verbatim to ``design_agent.revise``;
    it defaults to ``False`` when the caller omits it."""
    orch = StrategyLabOrchestrator()
    captured_kwargs: Dict[str, Any] = {}

    def _fake_revise(*_a: Any, **kwargs: Any) -> Any:
        captured_kwargs.update(kwargs)
        return _spec_dict(), "new rationale"

    monkeypatch.setattr(orch.design_agent, "revise", _fake_revise)
    critique = SpecCritique(ready=False, rationale="fix the entry")
    delta = SimpleNamespace(regressed=[])

    orch._revise_with_regression_notice(
        spec=_spec(),
        rationale="old rationale",
        critique=critique,
        delta=delta,
        critique_history=[critique],
        strategy_id="rebuilt-id",
        mechanical_repair_count=0,
        drift_collector=None,
    )
    assert captured_kwargs["skip_self_review"] is False

    orch._revise_with_regression_notice(
        spec=_spec(),
        rationale="old rationale",
        critique=critique,
        delta=delta,
        critique_history=[critique],
        strategy_id="rebuilt-id",
        mechanical_repair_count=0,
        drift_collector=None,
        skip_self_review=True,
    )
    assert captured_kwargs["skip_self_review"] is True
