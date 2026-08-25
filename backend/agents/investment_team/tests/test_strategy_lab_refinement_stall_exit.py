"""Refinement-loop stall exit.

Mirrors ``test_strategy_lab_max_rounds_exhaustion.py``'s shape, but for the
new within-loop stall guard: a stuck refinement (identical code against a
recurring failure) exits early with ``status="failed: refinement_stalled"``
instead of churning to ``MAX_CODE_REFINEMENT_ROUNDS``.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from investment_team.models import BacktestConfig
from investment_team.strategy_lab.orchestrator import (
    MAX_CODE_REFINEMENT_ROUNDS,
    StrategyLabOrchestrator,
)
from investment_team.strategy_lab.quality_gates.models import QualityGateResult
from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    IndicatorRef,
    Predicate,
    SignalExitRule,
)

pytestmark = pytest.mark.strategy_lab_integration


def _rsi_entry_dict() -> Dict[str, Any]:
    return EntryRule(
        side="long",
        when=Predicate(lhs=IndicatorRef(name="rsi", params={"period": 14}), op="<", rhs=30),
    ).model_dump()


def _rsi_exit_dict() -> Dict[str, Any]:
    return SignalExitRule(
        when=Predicate(lhs=IndicatorRef(name="rsi", params={"period": 14}), op=">", rhs=70),
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
        # No "asset_class": the design loop pins each attempt to one
        # randomly-selected category and an omitted class inherits that
        # pin, so this payload stays valid whichever category is drawn.
        "hypothesis": "RSI signal strategy",
        "signal_definition": "sig",
        "entry_rules": [_rsi_entry_dict()],
        "exit_rules": [_rsi_exit_dict()],
        "risk_limits": {"max_position_pct": 5, "max_drawdown_pct": 10},
        "speculative": False,
    }


def test_stuck_refinement_exits_early_with_stalled_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``noop_refine`` (unchanged code) against an identically-worded critical
    gate failure every round -> the stall guard breaks the loop well before
    the round cap, with ``status="failed: refinement_stalled"``."""
    from investment_team.tests.conftest import noop_refine, stub_design_loop

    orch = StrategyLabOrchestrator()
    stub_design_loop(monkeypatch, orch, _spec_dict(), _VALID_CODE)
    monkeypatch.setattr(orch.refinement_agent, "run", noop_refine(_VALID_CODE))

    def _always_critical(_code: str, _spec=None):
        return [
            QualityGateResult(
                gate_name="code_safety",
                passed=False,
                severity="critical",
                phase="synthesis",
                details="forced critical for test",
            )
        ]

    monkeypatch.setattr(orch.code_safety_checker, "check", _always_critical)

    record = orch.run_cycle(prior_records=[], config=_config())

    assert record.backtest.status == "failed: refinement_stalled"
    assert record.is_winning is False
    # Default STRATEGY_LAB_REFINEMENT_STALL_ROUNDS is 3 -> exits far short of
    # the round cap (49 attempts) rather than exhausting it.
    assert record.refinement_rounds < MAX_CODE_REFINEMENT_ROUNDS - 1


def test_stall_rounds_env_override_changes_when_it_trips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Raising ``STRATEGY_LAB_REFINEMENT_STALL_ROUNDS`` delays the stall exit
    by exactly that many additional rounds."""
    from investment_team.tests.conftest import noop_refine, stub_design_loop

    monkeypatch.setenv("STRATEGY_LAB_REFINEMENT_STALL_ROUNDS", "5")
    orch = StrategyLabOrchestrator()
    stub_design_loop(monkeypatch, orch, _spec_dict(), _VALID_CODE)
    monkeypatch.setattr(orch.refinement_agent, "run", noop_refine(_VALID_CODE))

    def _always_critical(_code: str, _spec=None):
        return [
            QualityGateResult(
                gate_name="code_safety",
                passed=False,
                severity="critical",
                phase="synthesis",
                details="forced critical for test",
            )
        ]

    monkeypatch.setattr(orch.code_safety_checker, "check", _always_critical)

    record = orch.run_cycle(prior_records=[], config=_config())

    assert record.backtest.status == "failed: refinement_stalled"
    # The stalling round itself returns before appending a new
    # refinement_attempts entry (no LLM call is made once a stall is
    # detected), so the persisted count is one less than the stall
    # threshold: attempts happen on the rounds *before* the signature has
    # repeated ``STRATEGY_LAB_REFINEMENT_STALL_ROUNDS`` times.
    assert record.refinement_rounds == 4
