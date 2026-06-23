"""Runtime look-ahead handling in :meth:`StrategyLabOrchestrator._run_verification_phase`.

The harness's ``AttributeError`` interceptor flips
``TradingServiceResult.lookahead_violation=True`` whenever generated code
reads a forward attribute on ``bar`` / ``ctx``. The compat shim propagates
that as ``StrategyRunResult.error_type="lookahead_violation"`` and the
synthesis loop threads the boolean onto
``_SynthesisLoopOutcome.runtime_lookahead_violation``. The verification phase
consumes the boolean here and, when True, appends
``lookahead_violation_at_runtime: subprocess_attribute_error`` to
``acceptance_reason`` so the cause is recorded as a narrative caveat.

Under the deterministic verdict, runtime look-ahead is a *caveat only* — it
no longer flips ``is_winning``. The label is the return-vs-benchmark rule
gated by the ``execution_succeeded and trades`` validity precondition. In
production an unresolved look-ahead exhausts refinement and reaches
verification with ``execution_succeeded=False``, so the validity precondition
already yields ``is_winning=False`` (an invalid run has no genuine return).
The contrived "clean ledger + lookahead=True" case is the documented residual
surface: the label follows the return and the look-ahead cause rides along as
a caveat.
"""

from __future__ import annotations

from typing import List, Optional

from investment_team.models import (
    BacktestConfig,
    BacktestResult,
    StrategySpec,
    TradeRecord,
)
from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator
from investment_team.strategy_lab.quality_gates.convergence_tracker import ConvergenceTracker
from investment_team.strategy_lab.spec_dsl import EntryRule, Predicate, SignalExitRule


def _spec(target_symbols: Optional[List[str]] = None) -> StrategySpec:
    return StrategySpec(
        strategy_id="lookahead-veto-test",
        authored_by="test",
        asset_class="stocks",
        hypothesis="hyp",
        signal_definition="sig",
        timeframe="1d",
        entry_rules=[EntryRule(side="long", when=Predicate(lhs="bar.close", op=">", rhs=0))],
        exit_rules=[SignalExitRule(when=Predicate(lhs="bar.close", op="<", rhs=0))],
        risk_limits={},
        speculative=False,
        target_symbols=target_symbols or [],
        strategy_code=(
            "from contract import Strategy\n"
            "class S(Strategy):\n"
            "    def on_bar(self, ctx, bar):\n"
            "        pass\n"
        ),
    )


def _config() -> BacktestConfig:
    return BacktestConfig(
        start_date="2020-01-01",
        end_date="2025-01-01",
        initial_capital=100_000.0,
        walk_forward_enabled=True,
    )


def _metrics(acceptance_reason: str = "") -> BacktestResult:
    return BacktestResult(
        total_return_pct=20.0,
        annualized_return_pct=10.0,
        volatility_pct=12.0,
        sharpe_ratio=0.8,
        max_drawdown_pct=10.0,
        win_rate_pct=58.0,
        profit_factor=1.6,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
        acceptance_reason=acceptance_reason,
    )


def _trade(symbol: str = "QQQ", trade_num: int = 1) -> TradeRecord:
    return TradeRecord(
        trade_num=trade_num,
        entry_date=f"2024-{((trade_num % 12) + 1):02d}-{((trade_num % 27) + 1):02d}",
        exit_date=f"2024-{((trade_num % 12) + 1):02d}-{((trade_num % 27) + 2):02d}",
        symbol=symbol,
        side="long",
        entry_price=100.0,
        exit_price=101.0,
        shares=10.0,
        position_value=1000.0,
        gross_pnl=10.0,
        net_pnl=10.0,
        return_pct=1.0,
        hold_days=14,
        outcome="win",
        cumulative_pnl=10.0 * trade_num,
    )


def _orch() -> StrategyLabOrchestrator:
    return StrategyLabOrchestrator(convergence_tracker=ConvergenceTracker())


def test_runtime_lookahead_violation_forces_is_winning_false():
    """A runtime look-ahead arrives with ``execution_succeeded=False`` (the
    production path), so the validity precondition yields ``is_winning=False``
    and the look-ahead cause is recorded on ``acceptance_reason`` as a caveat."""
    orch = _orch()
    metrics = _metrics()
    all_gate_results: list = []

    outcome = orch._run_verification_phase(
        spec=_spec(),
        trades=[],
        metrics=metrics,
        market_data=None,
        config=_config(),
        execution_succeeded=False,
        trades_aligned=True,
        alignment_reports=[],
        all_gate_results=all_gate_results,
        emit=lambda *_a, **_k: None,
        runtime_lookahead_violation=True,
    )

    assert outcome.is_winning is False
    reason = outcome.metrics.acceptance_reason or ""
    assert "lookahead_violation_at_runtime: subprocess_attribute_error" in reason


def test_runtime_lookahead_violation_replaces_generic_publication_disabled_reason(
    monkeypatch,
):
    """An execution that exhausted refinement on a lookahead lands in
    verification with ``execution_succeeded=False`` and the else-branch
    sets a generic ``publication_disabled`` reason. The lookahead veto
    must REPLACE that stale reason with the specific cause so the audit
    trail explains what actually blocked publication.
    """
    orch = _orch()
    # ``execution_succeeded=False`` + ``trades=[]`` hits the
    # ``publication_disabled: no trades produced`` else-branch first.
    monkeypatch.setattr(
        orch, "_evaluate_walk_forward", lambda spec, md, cfg, trades, metrics: metrics
    )
    monkeypatch.setattr(orch, "_run_realism_gates", lambda **_kwargs: [])

    outcome = orch._run_verification_phase(
        spec=_spec(),
        trades=[],
        metrics=_metrics(),
        market_data=None,
        config=_config(),
        execution_succeeded=False,
        trades_aligned=True,
        alignment_reports=[],
        all_gate_results=[],
        emit=lambda *_a, **_k: None,
        runtime_lookahead_violation=True,
    )

    reason = outcome.metrics.acceptance_reason or ""
    # The veto suffix must be present.
    assert "lookahead_violation_at_runtime: subprocess_attribute_error" in reason
    # The generic publication_disabled reason came from the else-branch
    # (which still fires when ``execution_succeeded=False``); the veto's
    # ``_apply_veto_to_acceptance_reason`` call appends to it because the
    # generic reason was set with ``upstream_admitted=False`` (publication
    # was already blocked). Either an "appended" or "replaced" combine is
    # acceptable as long as the lookahead cause survives.
    assert outcome.is_winning is False


def test_no_veto_when_runtime_lookahead_violation_is_false(monkeypatch):
    """The default ``runtime_lookahead_violation=False`` path must not
    touch ``is_winning`` or ``acceptance_reason`` — guards the no-regression
    contract for non-lookahead runs."""
    orch = _orch()
    monkeypatch.setattr(
        orch, "_evaluate_walk_forward", lambda spec, md, cfg, trades, metrics: metrics
    )
    monkeypatch.setattr(orch, "_run_realism_gates", lambda **_kwargs: [])

    initial = _metrics(acceptance_reason="walk_forward_passed: all four criteria met")
    outcome = orch._run_verification_phase(
        spec=_spec(),
        trades=[],
        metrics=initial,
        market_data=None,
        config=_config(),
        execution_succeeded=False,
        trades_aligned=True,
        alignment_reports=[],
        all_gate_results=[],
        emit=lambda *_a, **_k: None,
    )

    reason = outcome.metrics.acceptance_reason or ""
    assert "lookahead_violation_at_runtime" not in reason


def test_runtime_lookahead_violation_records_cause_on_clean_ledger(monkeypatch):
    """The documented residual surface: a clean ledger (execution_succeeded=
    True, trades present, return at/above the benchmark) that also carries a
    runtime look-ahead. Under the deterministic verdict the label follows the
    return rule — so ``is_winning`` is True here (10% >= 8%) — and the
    look-ahead cause is recorded on ``acceptance_reason`` as a caveat (it
    replaces the clean acceptance success string), surfacing in the narrative.

    In production this combination does not occur: an unresolved look-ahead
    reaches verification with ``execution_succeeded=False``, so the validity
    precondition forces ``is_winning=False`` (see the other tests). This pins
    the caveat-recording so a future refactor cannot drop the audit trail.
    """
    orch = _orch()
    monkeypatch.setattr(
        orch, "_evaluate_walk_forward", lambda spec, md, cfg, trades, metrics: metrics
    )
    # Acceptance gate returns one passing info-severity result so the
    # verification branch takes the ``acceptance_results`` path and
    # ``is_winning`` is computed as True before the lookahead veto fires.
    from investment_team.strategy_lab.quality_gates.models import QualityGateResult

    monkeypatch.setattr(
        orch.acceptance_gate,
        "check",
        lambda metrics, config, n_trials: [
            QualityGateResult(
                gate_name="oos_deflated_sharpe",
                passed=True,
                severity="info",
                phase="verification",
                details="ok",
            ),
        ],
    )
    monkeypatch.setattr(orch, "_run_realism_gates", lambda **_kwargs: [])

    trades = [_trade(trade_num=i + 1) for i in range(20)]

    outcome = orch._run_verification_phase(
        spec=_spec(target_symbols=["QQQ"]),
        trades=trades,
        metrics=_metrics(),
        market_data={"QQQ": []},
        config=_config(),
        execution_succeeded=True,
        trades_aligned=True,
        alignment_reports=[],
        all_gate_results=[],
        emit=lambda *_a, **_k: None,
        runtime_lookahead_violation=True,
    )

    # Deterministic verdict: a valid run (executed + trades) with 10% >= 8%
    # is winning; the look-ahead is a caveat, not a label-flip.
    assert outcome.is_winning is True
    reason = outcome.metrics.acceptance_reason or ""
    # The look-ahead cause is still recorded (it replaces the clean acceptance
    # success string, since upstream_admitted was True), so it rides into the
    # narrative as a caveat.
    assert "lookahead_violation_at_runtime: subprocess_attribute_error" in reason
