"""Cap-exhaustion status (#547 item 7).

The orchestrator's three ``MAX_CODE_REFINEMENT_ROUNDS`` break sites
(validation / execution / evaluation) now flip the persisted
``BacktestRecord.status`` to ``"failed: max_refinement_rounds"`` so
operators can query for exhausted cycles rather than scraping logs.

These tests force the loop to exhaust at each of the three sites and
assert on the final record's ``status``.
"""

from __future__ import annotations

import itertools
from typing import Any, Dict

import pytest

from investment_team.models import BacktestConfig
from investment_team.strategy_lab import orchestrator as orchestrator_module
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


def test_validation_phase_exhaustion_sets_max_rounds_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Code-safety always critical → loop exhausts validation phase.

    ``details`` varies per round (a real round counter, not a repeat) so
    the round-cap path is exercised genuinely rather than tripping the
    refinement-loop stall guard, which requires the ``(code,
    failure_details)`` signature to be identical across consecutive rounds.
    """
    from investment_team.tests.conftest import noop_refine, stub_design_loop

    orch = StrategyLabOrchestrator()
    stub_design_loop(monkeypatch, orch, _spec_dict(), _VALID_CODE)
    monkeypatch.setattr(orch.refinement_agent, "run", noop_refine(_VALID_CODE))

    round_counter = itertools.count()

    def _always_critical(_code: str, _spec=None):
        return [
            QualityGateResult(
                gate_name="code_safety",
                passed=False,
                severity="critical",
                phase="synthesis",
                details=f"forced critical for test (round {next(round_counter)})",
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
    """Sandbox always reports execution failure → loop exhausts execution phase.

    ``_VALID_CODE`` fails the real ``code_conformance_gate`` (missing
    ``ctx.indicator`` usage, literal ``qty=``), which would otherwise
    exhaust the loop at the VALIDATION phase before ever reaching execution;
    stub it to a clean pass so this test actually isolates the execute path.
    ``varying_code_refine`` (not ``noop_refine``) keeps each round's code
    hash distinct so ``BacktestCache`` doesn't freeze the execution failure
    to round 0's cached result, which would otherwise trip the
    refinement-loop stall guard instead of reaching genuine round-cap
    exhaustion.
    """
    from investment_team.tests.conftest import (
        empty_market_data,
        failing_sandbox,
        stub_design_loop,
        varying_code_refine,
    )

    orch = StrategyLabOrchestrator()
    stub_design_loop(monkeypatch, orch, _spec_dict(), _VALID_CODE)
    monkeypatch.setattr(StrategyLabOrchestrator, "_fetch_market_data", empty_market_data())
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
    monkeypatch.setattr(orch.refinement_agent, "run", varying_code_refine(_VALID_CODE))
    monkeypatch.setattr(orchestrator_module, "run_strategy_code", failing_sandbox())

    record = orch.run_cycle(prior_records=[], config=_config())

    assert record.backtest.status == "failed: max_refinement_rounds"
    assert record.is_winning is False


def test_evaluation_phase_exhaustion_does_not_mark_winner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even a high-return anomalous cycle that exhausts the round cap on
    evaluation must NOT be persisted with is_winning=True (#547 review feedback).
    Otherwise paper-trading would fire on a 'failed: max_refinement_rounds' record.

    ``_VALID_CODE`` fails the real ``code_conformance_gate``, which would
    otherwise exhaust the loop at the VALIDATION phase before ever reaching
    evaluation; stub it to a clean pass so this test actually isolates the
    evaluation path. ``varying_code_refine`` (not ``noop_refine``) keeps
    each round's code distinct so ``_cached_run_strategy_code``'s
    ``BacktestCache`` treats every round as a fresh execution instead of
    replaying round 0's cached result; the sandbox trades also vary per
    round (via ``offset``) so each round's ``(metrics, trades)`` genuinely
    differs. Both are needed so the anomaly-check cache (guarding
    ``anomaly_detector.check`` against redundant re-runs on an unchanged
    ledger) doesn't collapse rounds after the first into one cached
    verdict — the detector is still called fresh every round, and its
    ``details`` vary too, so the ``(code, failure_details)`` signature the
    stall guard tracks changes every round.
    """
    from investment_team.strategy_lab.quality_gates.models import QualityGateResult
    from investment_team.tests.conftest import (
        empty_market_data,
        stub_design_loop,
        varying_code_refine,
    )
    from investment_team.tests.test_strategy_lab_alignment import (
        _benign_sandbox_trades,
        _code_exec,
    )

    orch = StrategyLabOrchestrator()
    stub_design_loop(monkeypatch, orch, _spec_dict(), _VALID_CODE)
    monkeypatch.setattr(StrategyLabOrchestrator, "_fetch_market_data", empty_market_data())
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

    # Sandbox always succeeds with benign trades — execution itself is fine.
    # The ledger varies per round (via ``offset``) so the anomaly-check
    # cache sees a fresh ``(metrics, trades)`` signature every round instead
    # of reusing the first round's cached verdict.
    trade_round_counter = itertools.count()

    def _ok_sandbox(*_a, **_kw):
        return _code_exec(
            success=True, raw_trades=_benign_sandbox_trades(offset=next(trade_round_counter))
        )

    monkeypatch.setattr(orchestrator_module, "run_strategy_code", _ok_sandbox)
    monkeypatch.setattr(orch.refinement_agent, "run", varying_code_refine(_VALID_CODE))

    # Anomaly detector always reports critical → evaluation-phase exhaustion.
    # ``details`` varies per round so the round-cap path is exercised
    # genuinely rather than tripping the refinement-loop stall guard.
    round_counter = itertools.count()

    def _always_anomalous(*_a, **_kw):
        return [
            QualityGateResult(
                gate_name="backtest_anomaly",
                passed=False,
                severity="critical",
                phase="verification",
                details=f"forced anomaly for test (round {next(round_counter)})",
            )
        ]

    monkeypatch.setattr(orch.anomaly_detector, "check", _always_anomalous)

    record = orch.run_cycle(prior_records=[], config=_config())

    assert record.backtest.status == "failed: max_refinement_rounds"
    # The critical assertion: even if the cycle has trades and metrics
    # (because the sandbox kept succeeding), is_winning must stay False
    # because execution_succeeded was never flipped on the exhaustion path.
    assert record.is_winning is False
