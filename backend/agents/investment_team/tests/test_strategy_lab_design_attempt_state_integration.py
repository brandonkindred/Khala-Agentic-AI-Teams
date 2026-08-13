"""End-to-end verification that ``_DesignAttemptState`` threads correctly
across the design -> synthesis -> refinement -> verification pipeline.

The per-function migrations (design, verification, synthesis-evaluation,
anomaly-recovery) are each covered at the unit level elsewhere
(``test_orchestrator_helpers.py``, ``test_strategy_lab_synthesis_helpers.py``,
``test_strategy_lab_zero_trade_repair.py``). What's missing, and what this
module adds, is a real ``run_cycle`` drive through a fully converged (not
short-circuited) cycle that proves the same threaded state -- not just
equivalent per-call inputs -- flows unchanged across every phase boundary,
and lands intact in the persisted ``StrategyLabRecord``.
"""

from __future__ import annotations

from typing import Any, List

import pytest

from investment_team.models import BacktestResult, StrategyLabRecord
from investment_team.strategy_lab._orchestrator_helpers import _DesignAttemptState
from investment_team.strategy_lab.phases import PHASE_TRANSITION_EVENT_NAME, Phase

from ._walk_forward_test_helpers import (
    StubMarketDataService,
    orchestrator,
    wire_run_cycle_stubs,
)
from ._walk_forward_test_helpers import config as _config

pytestmark = pytest.mark.strategy_lab_integration


def test_design_attempt_state_threads_end_to_end_on_converged_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fully converged ``run_cycle`` (real synthetic bars, a succeeding
    sandbox execution with real trades, an aligned ledger on round 0) drives
    spec/code/trades/metrics through ``_DesignAttemptState`` at every
    construction site the happy path reaches -- the synthesis loop's
    per-round evaluation, verification, and record assembly -- proving the
    migration threads state correctly across phase boundaries rather than
    only within isolated unit calls."""
    orch = orchestrator(StubMarketDataService())

    # Spy on every bare _DesignAttemptState(...) construction. Its five
    # dataclass subclasses each get their own generated __init__ (a
    # dataclass subclass's generated __init__ sets inherited + own fields
    # directly; it does not call the base's __init__), so this spy only
    # observes constructions of the bare base class itself -- exactly the
    # four call sites (orchestrator_design.py:1354,1394 and
    # orchestrator_synthesis.py:538,952) that thread the migrated state.
    constructed: List[_DesignAttemptState] = []
    original_init = _DesignAttemptState.__init__

    def _spy_init(self: _DesignAttemptState, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        constructed.append(self)

    monkeypatch.setattr(_DesignAttemptState, "__init__", _spy_init)

    wire_run_cycle_stubs(orch, monkeypatch, alignment_aligned=True)

    # Capture what the analysis agent actually receives -- a downstream
    # consumer of the threaded state's final spec/trades -- so the test can
    # confirm nothing silently substituted a stale or re-derived copy on
    # the way out of the pipeline. analysis_agent.run's spec/trades are
    # passed positionally by the orchestrator, so a plain args-and-kwargs
    # recorder is used rather than the shared kwargs-only test double.
    # This override must come after wire_run_cycle_stubs, which installs
    # its own default analysis_agent.run stub.
    analysis_calls: List[Any] = []
    monkeypatch.setattr(
        orch.analysis_agent,
        "run",
        lambda *args, **kwargs: analysis_calls.append((args, kwargs)) or "narrative",
    )

    # wire_run_cycle_stubs patches compute_metrics to a single constant
    # BacktestResult regardless of the trades it's called with, which would
    # make the two real compute_metrics call sites indistinguishable here:
    # the loop's zero-trade initialization (orchestrator_synthesis.py:349)
    # and the post-execution round evaluation (orchestrator_synthesis.py:
    # 920) would both hand back the same object, so a regression that
    # threaded the stale zero-trade placeholder into verification would
    # still pass a bare `is not None` check. Override with distinct
    # sentinels keyed on whether any trades were passed, so the assertions
    # below can tell "stale init placeholder" apart from "this round's
    # real result" and confirm the pipeline forwards the latter.
    empty_ledger_metrics = BacktestResult(
        total_return_pct=0.0,
        annualized_return_pct=0.0,
        volatility_pct=0.0,
        sharpe_ratio=0.0,
        max_drawdown_pct=0.0,
        win_rate_pct=0.0,
        profit_factor=0.0,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
    )
    round_metrics = BacktestResult(
        total_return_pct=18.0,
        annualized_return_pct=15.0,
        volatility_pct=8.0,
        sharpe_ratio=1.4,
        max_drawdown_pct=4.0,
        win_rate_pct=60.0,
        profit_factor=2.0,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
    )
    monkeypatch.setattr(
        "investment_team.strategy_lab.orchestrator.compute_metrics",
        lambda trades, *args, **kwargs: round_metrics if trades else empty_ledger_metrics,
    )

    config = _config(walk_forward_enabled=False)
    record: StrategyLabRecord = orch.run_cycle(prior_records=[], config=config)

    # -- the cycle actually converged (not a short-circuit) --------------
    assert record.backtest.status == "completed"
    assert record.is_winning is True
    assert record.backtest.trades, "a converged cycle must carry real trades"

    # -- every reachable construction site fired for real ----------------
    # _handle_critical_anomalies's construction (orchestrator_synthesis.py:
    # 952) is conditioned on a critical-anomaly gate firing, which a clean
    # winning round-0 run never trips; that site is already exercised with
    # a real _DesignAttemptState by test_strategy_lab_zero_trade_repair.py
    # and test_strategy_lab_synthesis_helpers.py.
    assert len(constructed) == 3, (
        f"expected exactly 3 _DesignAttemptState constructions on a clean "
        f"one-round convergence (synthesis-round evaluation, pre-verification, "
        f"record assembly), got {len(constructed)}"
    )
    synthesis_round_state, pre_verification_state, record_assembly_state = constructed

    # -- the same spec/code/trades flow unchanged through every site -----
    # spec is never copied by the synthesis/alignment/verification code
    # this scenario exercises, so the exact same object should be visible
    # at every downstream site once the synthesis round settles.
    assert pre_verification_state.spec is synthesis_round_state.spec
    assert record_assembly_state.spec is pre_verification_state.spec
    assert pre_verification_state.code == synthesis_round_state.code
    assert record_assembly_state.code == pre_verification_state.code
    assert record_assembly_state.trades == synthesis_round_state.trades
    assert pre_verification_state.trades == synthesis_round_state.trades

    # metrics: the synthesis-round state's ``.metrics`` field really is the
    # stale zero-trade placeholder seeded before the loop started (per
    # _evaluate_synthesis_round's own precondition that it computes fresh
    # metrics from state.trades rather than trusting state.metrics) --
    # confirmed here via the distinct sentinel rather than a bare
    # not-None check, so a regression that threaded the stale placeholder
    # into verification would actually fail this assertion.
    assert synthesis_round_state.metrics is empty_ledger_metrics
    # pre_verification carries this round's freshly computed BacktestResult
    # untouched.
    assert pre_verification_state.metrics is round_metrics
    # record_assembly's metrics is verification's own annotated copy (it
    # stamps fields like acceptance_reason onto the BacktestResult, so it's
    # no longer the exact same object) -- assert it's derived from this
    # round's real result rather than the stale placeholder by comparing
    # the field the two sentinels disagree on, then confirm it matches
    # what the final record persists.
    assert record_assembly_state.metrics.total_return_pct == round_metrics.total_return_pct
    assert record_assembly_state.metrics.total_return_pct != empty_ledger_metrics.total_return_pct
    assert record_assembly_state.metrics == record.backtest.result

    # -- the persisted record matches the threaded state exactly ---------
    assert record.strategy.strategy_id == record_assembly_state.spec.strategy_id
    assert record.strategy_code == record_assembly_state.code
    assert record.backtest.trades == record_assembly_state.trades

    # -- a downstream consumer saw the same trades/spec the threaded
    #    state carried, not a stale or re-derived copy -------------------
    assert analysis_calls, "AnalysisAgent.run must have been invoked on a winning cycle"
    analysis_args, _analysis_kwargs = analysis_calls[0]
    analysis_spec, _analysis_metrics, analysis_trades, _analysis_rationale = analysis_args[:4]
    assert analysis_spec is record_assembly_state.spec
    assert analysis_trades == record_assembly_state.trades


def test_phase_transitions_stable_on_converged_cycle(monkeypatch: pytest.MonkeyPatch) -> None:
    """The four-phase ``phase_transition`` boundary contract (asserted for
    the short-circuited path in ``test_strategy_lab_phase_transitions.py``)
    also holds on a fully converged run -- design/synthesis/verification all
    execute for real rather than no-op'ing on ``execution_succeeded=False``."""
    orch = orchestrator(StubMarketDataService())
    wire_run_cycle_stubs(orch, monkeypatch, alignment_aligned=True)

    events: List[Any] = []
    config = _config(walk_forward_enabled=False)
    record = orch.run_cycle(
        prior_records=[],
        config=config,
        on_phase=lambda phase, data: events.append((phase, data)),
    )
    transitions = [data for phase, data in events if phase == PHASE_TRANSITION_EVENT_NAME]
    actual = [(t["from_phase"], t["to_phase"]) for t in transitions]
    assert actual == [
        (Phase.DESIGN.value, Phase.DESIGN_REVIEW.value),
        (Phase.DESIGN_REVIEW.value, Phase.CODE_SYNTHESIS.value),
        (Phase.CODE_SYNTHESIS.value, Phase.BACKTEST_AND_VERIFICATION.value),
        (Phase.BACKTEST_AND_VERIFICATION.value, None),
    ]
    assert record.backtest.status == "completed"
    assert record.is_winning is True
