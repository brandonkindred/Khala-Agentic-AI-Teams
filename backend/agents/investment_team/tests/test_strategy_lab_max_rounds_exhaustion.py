"""Cap-exhaustion status (#547 item 7).

The orchestrator's three ``MAX_CODE_REFINEMENT_ROUNDS`` break sites
(validation / execution / evaluation) now flip the persisted
``BacktestRecord.status`` to ``"failed: max_refinement_rounds"`` so
operators can query for exhausted cycles rather than scraping logs.

These tests force the loop to exhaust at each of the three sites and
assert on the final record's ``status``.
"""

from __future__ import annotations

from typing import Any, Dict, Tuple

import pytest

from investment_team.models import BacktestConfig
from investment_team.strategy_lab import orchestrator as orchestrator_module
from investment_team.strategy_lab.orchestrator import (
    MAX_CODE_REFINEMENT_ROUNDS,
    StrategyLabOrchestrator,
)
from investment_team.strategy_lab.quality_gates.models import QualityGateResult
from investment_team.strategy_lab.spec_dsl import (
    ConstRef,
    EntryRule,
    Predicate,
    RSIRef,
    SignalExitRule,
)


def _rsi_entry_dict() -> Dict[str, Any]:
    return EntryRule(
        side="long",
        when=Predicate(lhs=RSIRef(period=14), op="lt", rhs=ConstRef(value=30)),
    ).model_dump()


def _rsi_exit_dict() -> Dict[str, Any]:
    return SignalExitRule(
        when=Predicate(lhs=RSIRef(period=14), op="gt", rhs=ConstRef(value=70)),
    ).model_dump()


def _config() -> BacktestConfig:
    return BacktestConfig(
        start_date="2023-01-01",
        end_date="2023-12-31",
        initial_capital=100_000.0,
        benchmark_symbol="SPY",
        transaction_cost_bps=5.0,
        slippage_bps=2.0,
    )


_VALID_CODE = (
    "from contract import Strategy\n\n"
    "class S(Strategy):\n"
    "    def on_bar(self, ctx, bar):\n"
    "        ctx.submit_order(symbol='X', qty=1, side='LONG')\n"
    "        ctx.submit_order(symbol='X', qty=1, side='FLAT')\n"
)


def _spec_dict() -> Dict[str, Any]:
    return {
        "asset_class": "stocks",
        "hypothesis": "RSI signal strategy",
        "signal_definition": "sig",
        "entry_rules": [_rsi_entry_dict()],
        "exit_rules": [_rsi_exit_dict()],
        "risk_limits": {"max_position_pct": 5, "max_drawdown_pct": 10},
        "speculative": False,
    }


def _ideation_returning(d: Dict[str, Any], code: str) -> Any:
    def _run(**_kwargs) -> Tuple[Dict[str, Any], str, str]:
        return d, code, "scripted rationale"

    return _run


def _stub_market_data(*_a, **_kw):
    # Issue #525 — orchestrator now returns a _MarketDataFetch envelope.
    from investment_team.strategy_lab.orchestrator import _MarketDataFetch

    return _MarketDataFetch(
        data={"AAPL": []},
        requested_symbols=["AAPL"],
        fetched_symbols=[],
    )


def test_validation_phase_exhaustion_sets_max_rounds_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Code-safety always critical → loop exhausts validation phase."""
    orch = StrategyLabOrchestrator()
    monkeypatch.setattr(orch.ideation_agent, "run", _ideation_returning(_spec_dict(), _VALID_CODE))

    # Refinement is a no-op: same code back, so each round re-fails code-safety.
    def _stub_refine(**_kw):
        return {"changes_made": "no-op"}, _VALID_CODE

    monkeypatch.setattr(orch.refinement_agent, "run", _stub_refine)

    def _always_critical(_code: str, _spec=None):
        return [
            QualityGateResult(
                gate_name="code_safety",
                passed=False,
                severity="critical",
                details="forced critical for test",
            )
        ]

    monkeypatch.setattr(orch.code_safety_checker, "check", _always_critical)

    record = orch.run_cycle(prior_records=[], config=_config())

    assert record.backtest.status == "failed: max_refinement_rounds"
    assert record.is_winning is False
    # Refinement is called on every round except the final one (which falls
    # into the else-break branch), so attempts == MAX_CODE_REFINEMENT_ROUNDS - 1.
    assert record.refinement_rounds == MAX_CODE_REFINEMENT_ROUNDS - 1


def test_execution_phase_exhaustion_sets_max_rounds_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sandbox always reports execution failure → loop exhausts execution phase."""
    from investment_team.trading_service.modes.sandbox_compat import StrategyRunResult

    orch = StrategyLabOrchestrator()
    monkeypatch.setattr(orch.ideation_agent, "run", _ideation_returning(_spec_dict(), _VALID_CODE))
    monkeypatch.setattr(StrategyLabOrchestrator, "_fetch_market_data", _stub_market_data)

    def _stub_refine(**_kw):
        return {"changes_made": "no-op"}, _VALID_CODE

    monkeypatch.setattr(orch.refinement_agent, "run", _stub_refine)

    def _always_fails(*_a, **_kw):
        return StrategyRunResult(
            success=False,
            trades=[],
            stderr="forced failure for test",
            error_type="runtime_error",
        )

    monkeypatch.setattr(orchestrator_module, "run_strategy_code", _always_fails)

    record = orch.run_cycle(prior_records=[], config=_config())

    assert record.backtest.status == "failed: max_refinement_rounds"
    assert record.is_winning is False


def test_evaluation_phase_exhaustion_does_not_mark_winner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even a high-return anomalous cycle that exhausts the round cap on
    evaluation must NOT be persisted with is_winning=True (#547 review feedback).
    Otherwise paper-trading would fire on a 'failed: max_refinement_rounds' record."""
    from investment_team.strategy_lab.quality_gates.models import QualityGateResult
    from investment_team.tests.test_strategy_lab_alignment import (
        _benign_sandbox_trades,
        _code_exec,
    )

    orch = StrategyLabOrchestrator()
    monkeypatch.setattr(orch.ideation_agent, "run", _ideation_returning(_spec_dict(), _VALID_CODE))
    monkeypatch.setattr(StrategyLabOrchestrator, "_fetch_market_data", _stub_market_data)

    # Sandbox always succeeds with benign trades — execution itself is fine.
    def _ok_sandbox(*_a, **_kw):
        return _code_exec(success=True, raw_trades=_benign_sandbox_trades())

    monkeypatch.setattr(orchestrator_module, "run_strategy_code", _ok_sandbox)

    # Refinement is a no-op so the loop keeps hitting the same anomaly.
    def _stub_refine(**_kw):
        return {"changes_made": "no-op"}, _VALID_CODE

    monkeypatch.setattr(orch.refinement_agent, "run", _stub_refine)

    # Anomaly detector always reports critical → evaluation-phase exhaustion.
    def _always_anomalous(*_a, **_kw):
        return [
            QualityGateResult(
                gate_name="backtest_anomaly",
                passed=False,
                severity="critical",
                details="forced anomaly for test",
            )
        ]

    monkeypatch.setattr(orch.anomaly_detector, "check", _always_anomalous)

    record = orch.run_cycle(prior_records=[], config=_config())

    assert record.backtest.status == "failed: max_refinement_rounds"
    # The critical assertion: even if the cycle has trades and metrics
    # (because the sandbox kept succeeding), is_winning must stay False
    # because execution_succeeded was never flipped on the exhaustion path.
    assert record.is_winning is False
