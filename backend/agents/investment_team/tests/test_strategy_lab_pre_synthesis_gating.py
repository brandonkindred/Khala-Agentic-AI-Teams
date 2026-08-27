"""Pre-synthesis spec gating in the Strategy Lab orchestrator.

The orchestrator runs ``StrategySpecValidator`` once, immediately after
the design loop converges and before the refinement loop. Critical
failures short-circuit the cycle: no sandbox call, no market-data fetch,
and the persisted record carries ``status="failed: spec_validation"``.

These tests stub the new design pipeline (``DesignAgent`` +
``DesignReviewAgent`` + ``compile_strategy``) and patch
``orchestrator_module.run_strategy_code`` so a failure trips the new
short-circuit and the test fails loudly if the orchestrator ever calls
the sandbox.
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
    StopLossRule,
)
from investment_team.tests.conftest import stub_design_loop

# Every test in this module drives `run_cycle` on a real
# StrategyLabOrchestrator; the marker auto-applies the readiness fetch
# stub from conftest. See conftest.py for the contract.
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


def _stop_loss_exit_dict() -> Dict[str, Any]:
    return StopLossRule(pct=0.03).model_dump()


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


def test_pre_synthesis_critical_failure_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty entry_rules trips the design-phase readiness gate → cycle exits
    without sandbox call.

    The split design pipeline moves the readiness check into the design ↔
    review loop, so a spec with no entry rules now fails ``design_not_ready``
    (or ``design_stalled`` — the identical readiness critical repeats every
    round, which the within-loop stall guard detects) rather than the
    post-design ``spec_validation`` short-circuit. Either way the contract is
    the same: no code execution, no market-data fetch.
    """
    bad_spec_dict = {
        # No "asset_class": the design loop pins each attempt to one
        # randomly-selected category and an omitted class inherits that
        # pin, so this payload stays valid whichever category is drawn.
        "hypothesis": "test",
        "signal_definition": "sig",
        "entry_rules": [],  # critical: no entry rules
        "exit_rules": [_stop_loss_exit_dict()],
        "risk_limits": {"max_position_pct": 5, "max_drawdown_pct": 10},
        "speculative": False,
    }

    orch = StrategyLabOrchestrator()
    stub_design_loop(monkeypatch, orch, bad_spec_dict, _VALID_CODE)

    def _sandbox_must_not_run(*_a, **_kw):
        raise AssertionError("run_strategy_code must not be called when design gating fails")

    monkeypatch.setattr(orchestrator_module, "run_strategy_code", _sandbox_must_not_run)

    def _market_data_must_not_run(self, *_a, **_kw):
        raise AssertionError("_fetch_market_data must not be called when design gating fails")

    monkeypatch.setattr(StrategyLabOrchestrator, "_fetch_market_data", _market_data_must_not_run)

    record = orch.run_cycle(prior_records=[], config=_config())

    # "design_not_ready" / "design_stalled" (caught in the new design loop)
    # or "spec_validation" (caught by the legacy pre-synthesis gate) are all
    # valid short-circuit outcomes — each proves the sandbox was not touched.
    assert record.backtest.status in {
        "failed: design_not_ready",
        "failed: design_stalled",
        "failed: spec_validation",
    }
    assert record.is_winning is False
    assert record.refinement_rounds == 0
    # The design loop persists readiness findings with refinement_round=-1
    # alongside the new ``critiques`` audit trail.
    pre_synth_gates = [g for g in record.quality_gate_results if g.get("refinement_round") == -1]
    assert pre_synth_gates, record.quality_gate_results
    assert any(g.get("severity") == "critical" and not g.get("passed") for g in pre_synth_gates), (
        pre_synth_gates
    )


def test_pre_synthesis_validator_persisted_even_on_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid spec records the pre-synthesis pass result and enters the loop."""
    valid_spec_dict = {
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

    orch = StrategyLabOrchestrator()
    stub_design_loop(monkeypatch, orch, valid_spec_dict, _VALID_CODE)

    # The orchestrator will then enter the refinement loop. Short-circuit on
    # the first failed code_safety check so the test does not need a full
    # sandbox stack — record stays in scope to assert on the pre-synth gates.
    # Return a _MarketDataFetch envelope with data=None to
    # trigger the no-market-data short-circuit.
    from investment_team.strategy_lab.orchestrator import _MarketDataFetch

    monkeypatch.setattr(
        StrategyLabOrchestrator,
        "_fetch_market_data",
        lambda *_a, **_kw: _MarketDataFetch(data=None, requested_symbols=[], fetched_symbols=[]),
    )

    record = orch.run_cycle(prior_records=[], config=_config())

    pre_synth_gates = [g for g in record.quality_gate_results if g.get("refinement_round") == -1]
    assert pre_synth_gates, record.quality_gate_results
    # All pre-synthesis gates passed (or are info-severity) — none should be critical.
    assert not any(
        g.get("severity") == "critical" and not g.get("passed") for g in pre_synth_gates
    ), pre_synth_gates


def test_missing_strategy_code_does_not_short_circuit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ideation may return an empty ``strategy_code``. That's a
    code-generation failure that the refinement loop's existing code-
    safety + regeneration paths are equipped to repair; the pre-synthesis
    spec gate must NOT short-circuit on it (regressing a previously-
    recoverable case into an outright failure).

    The validator's ``strategy_code is missing — nothing to execute``
    critical is excluded from short-circuit eligibility; the cycle
    proceeds into the refinement loop.
    """
    valid_spec_dict_but_no_code = {
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

    orch = StrategyLabOrchestrator()
    # Empty code string — the deterministic compiler will still synthesise
    # canonical code for a valid spec, so we explicitly stub
    # ``compile_strategy`` to "" via the design-loop helper to mirror the
    # legacy "ideation produced empty code" path.
    stub_design_loop(monkeypatch, orch, valid_spec_dict_but_no_code, "")
    # Short-circuit further down by returning no market data so the cycle
    # exits cleanly without needing a full sandbox stack. The fetch
    # path returns a _MarketDataFetch envelope.
    from investment_team.strategy_lab.orchestrator import _MarketDataFetch

    monkeypatch.setattr(
        StrategyLabOrchestrator,
        "_fetch_market_data",
        lambda *_a, **_kw: _MarketDataFetch(data=None, requested_symbols=[], fetched_symbols=[]),
    )

    record = orch.run_cycle(prior_records=[], config=_config())

    # The cycle did NOT short-circuit with "failed: spec_validation":
    # the validator's strategy_code critical was excluded from pre-synth
    # gating, so we proceeded past it.
    assert record.backtest.status != "failed: spec_validation", record.backtest.status

    # And the gate must not be persisted as an unresolved critical on
    # the record — the refinement loop's code-safety + regeneration
    # paths repair it, so leaving it on the record would create a
    # permanently-unresolved spec critical (the generic refinement loop
    # never re-runs StrategySpecValidator). Filter the pre-synth slice.
    pre_synth_gates = [g for g in record.quality_gate_results if g.get("refinement_round") == -1]
    assert not any(
        g.get("severity") == "critical"
        and g.get("details", "").startswith("strategy_code is missing")
        for g in pre_synth_gates
    ), pre_synth_gates


def test_spec_unimplementable_exhaustion_preserves_design_telemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cycle that converges design but trips ``SpecImplementabilityError`` in
    the refinement loop on every re-entry short-circuits with
    ``failed: spec_unimplementable`` — and the persisted ``loop_telemetry``
    must carry the design-loop fields (round count + stop reason) of the
    attempt that ran, not the empty default the bypass path would record.
    """
    from investment_team.strategy_lab.exceptions import SpecImplementabilityError

    valid_spec_dict = {
        # No "asset_class": the design loop pins each attempt to one
        # randomly-selected category and an omitted class inherits that
        # pin, so this payload stays valid whichever category is drawn.
        "hypothesis": "RSI signal strategy",
        "signal_definition": "sig",
        "entry_rules": [_rsi_entry_dict()],
        "exit_rules": [_rsi_exit_dict(), _stop_loss_exit_dict()],
        "risk_limits": {"max_position_pct": 5, "max_drawdown_pct": 10},
        "target_symbols": ["QQQ"],
        "speculative": False,
    }

    orch = StrategyLabOrchestrator()
    stub_design_loop(monkeypatch, orch, valid_spec_dict, _VALID_CODE)

    # The refinement loop trips re-design on every attempt. The orchestrator
    # exhausts MAX_DESIGN_REENTRIES and short-circuits spec_unimplementable.
    def _always_unimplementable(*_a, **kwargs):
        spec = kwargs["spec"]
        raise SpecImplementabilityError(
            "forced unimplementable for test",
            failure_phase="execution",
            last_spec=spec,
            last_code=kwargs.get("code", ""),
        )

    monkeypatch.setattr(orch, "_run_synthesis_loop", _always_unimplementable)

    record = orch.run_cycle(prior_records=[], config=_config())

    assert record.backtest.status == "failed: spec_unimplementable"
    # The design loop converged each attempt (review ready on round 1), so the
    # telemetry must reflect that — not an unknown/missing stop reason.
    telemetry = record.loop_telemetry
    assert telemetry.get("stop_reason") == "ready"
    assert telemetry.get("design_review_rounds", 0) >= 1
    assert "critique_ledger" in telemetry
