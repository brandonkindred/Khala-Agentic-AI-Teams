"""Per-cycle LLM-call budget now guards the refinement/alignment tail (#1569).

Mirrors ``test_strategy_lab_design_loop.py::test_budget_exhausted_short_circuits_with_status``'s
shape, but for the code-refinement and trade-alignment loops rather than the
design phase: a stub that calls ``charge_active_budget()`` itself (simulating
a real LLM round-trip) trips ``DesignBudgetExhausted`` mid-loop, and the new
catch site in ``_run_design_attempt`` converts it to the same
``status="failed: budget_exhausted"`` short-circuit the design phase produces.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from investment_team.models import BacktestConfig
from investment_team.strategy_lab import orchestrator as orchestrator_module
from investment_team.strategy_lab.agents._llm_budget import charge_active_budget
from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator
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


def test_refinement_budget_exhaustion_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refinement agent that charges the active budget itself trips
    ``DesignBudgetExhausted`` on its second call; the orchestrator's ``_refine``
    now re-raises (rather than swallowing it into "no changes"), and the new
    catch site around ``_orchestrate_refinement_and_alignment`` converts the
    escaped exception into ``status="failed: budget_exhausted"`` instead of
    letting it crash ``run_cycle`` or churning to the round cap / stall exit."""
    from investment_team.tests.conftest import stub_design_loop

    # ``stub_design_loop`` wires design_agent.run/revise as plain stubs that
    # do not call charge_active_budget(), so the design phase consumes none
    # of this budget — the full limit is available for refinement.
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_MAX_LLM_CALLS", "1")
    orch = StrategyLabOrchestrator()
    stub_design_loop(monkeypatch, orch, _spec_dict(), _VALID_CODE)

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

    def _charging_refine(**_kw):
        charge_active_budget()
        return {"changes_made": "no-op"}, _VALID_CODE

    monkeypatch.setattr(orch.refinement_agent, "run", _charging_refine)

    def _market_must_not_run(self, *_a, **_kw):
        raise AssertionError("synthesis must not fetch market data once the budget trips")

    monkeypatch.setattr(StrategyLabOrchestrator, "_fetch_market_data", _market_must_not_run)

    record = orch.run_cycle(prior_records=[], config=_config())

    assert record.backtest.status == "failed: budget_exhausted"
    assert record.is_winning is False


def test_alignment_budget_exhaustion_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Synthesis converges cleanly (execution succeeds, no anomalies), then a
    deterministically-misaligned trade ledger drives the alignment loop into
    ``propose_code_fix`` every round. A stub that charges the budget itself
    trips ``DesignBudgetExhausted`` on its second call; the fix in
    ``_run_alignment_audit``/``propose_code_fix`` (re-raise instead of
    fail-closed-wrapping into ``AlignmentAuditError``) lets it escape to the
    same new catch site, converting it to ``status="failed: budget_exhausted"``."""
    from investment_team.strategy_lab.agents.alignment import TradeAlignmentReport
    from investment_team.tests.conftest import empty_market_data, stub_design_loop
    from investment_team.tests.test_strategy_lab_alignment import (
        _benign_sandbox_trades,
        _code_exec,
        _misaligned_check_result,
    )

    monkeypatch.setenv("STRATEGY_LAB_DESIGN_MAX_LLM_CALLS", "1")
    orch = StrategyLabOrchestrator()
    stub_design_loop(monkeypatch, orch, _spec_dict(), _VALID_CODE)
    monkeypatch.setattr(StrategyLabOrchestrator, "_fetch_market_data", empty_market_data())

    # Validation and execution both pass cleanly and deterministically so the
    # synthesis loop reaches alignment with execution_succeeded=True.
    monkeypatch.setattr(
        orch.code_conformance_gate,
        "check",
        lambda _code, _spec=None: [
            QualityGateResult(
                gate_name="code_conformance",
                passed=True,
                severity="info",
                phase="synthesis",
                details="stubbed pass",
            )
        ],
    )

    def _ok_sandbox(*_a, **_kw):
        return _code_exec(success=True, raw_trades=_benign_sandbox_trades())

    monkeypatch.setattr(orchestrator_module, "run_strategy_code", _ok_sandbox)

    # Deterministic alignment gate reports the ledger misaligned every round —
    # no near-miss adjudication involved, so no budget is consumed here.
    monkeypatch.setattr(
        orch.deterministic_alignment_checker,
        "check",
        lambda **_kw: _misaligned_check_result(),
    )

    def _charging_propose_fix(**_kw):
        # Charges once per call, exactly as the real agent does: the first
        # call succeeds (budget limit is 1) and returns a proposal that
        # keeps the loop going (unaligned, with a patch to retry); the
        # second call's charge trips DesignBudgetExhausted.
        charge_active_budget()
        return TradeAlignmentReport(
            aligned=False, proposed_code=_VALID_CODE, rationale="scripted, still misaligned"
        )

    monkeypatch.setattr(orch.alignment_agent, "propose_code_fix", _charging_propose_fix)

    record = orch.run_cycle(prior_records=[], config=_config())

    assert record.backtest.status == "failed: budget_exhausted"
    assert record.is_winning is False
