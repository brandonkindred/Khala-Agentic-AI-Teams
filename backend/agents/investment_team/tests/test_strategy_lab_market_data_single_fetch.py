"""Regression: market data is fetched exactly once per design attempt.

The synthesis loop hoists the fetch behind an ``if market_data is None``
guard and threads the same ``market_data`` object through every refinement
round, the alignment loop, and the audit. This test locks that contract in
by forcing the loop to run the full ``MAX_CODE_REFINEMENT_ROUNDS`` (execution
fails every round) while counting ``_fetch_market_data`` invocations.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from investment_team.models import BacktestConfig
from investment_team.strategy_lab import orchestrator as orchestrator_module
from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator
from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    IndicatorRef,
    Predicate,
    SignalExitRule,
)

pytestmark = pytest.mark.strategy_lab_integration


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
        "entry_rules": [
            EntryRule(
                side="long",
                when=Predicate(lhs=IndicatorRef(name="rsi", params={"period": 14}), op="<", rhs=30),
            ).model_dump()
        ],
        "exit_rules": [
            SignalExitRule(
                when=Predicate(lhs=IndicatorRef(name="rsi", params={"period": 14}), op=">", rhs=70),
            ).model_dump()
        ],
        "risk_limits": {"max_position_pct": 5, "max_drawdown_pct": 10},
        "speculative": False,
    }


def _config() -> BacktestConfig:
    return BacktestConfig(
        start_date="2023-01-01",
        end_date="2023-12-31",
        initial_capital=100_000.0,
        transaction_cost_bps=5.0,
        slippage_bps=2.0,
    )


def test_market_data_fetched_once_across_refinement_rounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from investment_team.strategy_lab.quality_gates.models import QualityGateResult
    from investment_team.tests.conftest import (
        empty_market_data,
        noop_refine,
        stub_design_loop,
    )
    from investment_team.tests.test_strategy_lab_alignment import (
        _benign_sandbox_trades,
        _code_exec,
    )

    orch = StrategyLabOrchestrator()
    stub_design_loop(monkeypatch, orch, _spec_dict(), _VALID_CODE)

    # Count fetches while delegating to the standard non-empty stub envelope.
    # The loop reaches the fetch step (execution succeeds) and is driven to
    # full round-cap exhaustion by an always-critical anomaly verdict.
    underlying = empty_market_data()
    fetch_calls = {"n": 0}

    def _counting_fetch(self, *args, **kwargs):
        fetch_calls["n"] += 1
        return underlying(self, *args, **kwargs)

    def _ok_sandbox(*_a, **_kw):
        return _code_exec(success=True, raw_trades=_benign_sandbox_trades())

    def _always_anomalous(*_a, **_kw):
        return [
            QualityGateResult(
                gate_name="backtest_anomaly",
                passed=False,
                severity="critical",
                phase="verification",
                details="forced anomaly for test",
            )
        ]

    # Let synthesis-phase validation pass so the loop reaches the fetch +
    # execute steps; the always-critical anomaly then drives round-cap
    # exhaustion in the evaluation phase.
    monkeypatch.setattr(orch.spec_readiness_gate, "validate", lambda *a, **kw: [])
    monkeypatch.setattr(orch.code_safety_checker, "check", lambda *a, **kw: [])
    monkeypatch.setattr(orch.code_conformance_gate, "check", lambda *a, **kw: [])
    monkeypatch.setattr(StrategyLabOrchestrator, "_fetch_market_data", _counting_fetch)
    monkeypatch.setattr(orch.refinement_agent, "run", noop_refine(_VALID_CODE))
    monkeypatch.setattr(orchestrator_module, "run_strategy_code", _ok_sandbox)
    monkeypatch.setattr(orch.anomaly_detector, "check", _always_anomalous)

    record = orch.run_cycle(prior_records=[], config=_config())

    # Loop ran to exhaustion (many rounds) but data was fetched exactly once.
    assert record.backtest.status == "failed: max_refinement_rounds"
    assert record.refinement_rounds > 1  # confirm the loop actually iterated
    assert fetch_calls["n"] == 1
