"""Strategy Lab Orchestrator — deterministic pipeline for code-generation backtesting.

Pipeline:
1. Strands Agent ideates strategy + generates Python code
2. Code refinement loop (up to 50 rounds): validate spec & code safety,
   execute in sandbox, fix syntax/build/runtime errors until the code
   runs cleanly and produces valid trade output
3. Backtest evaluation: compute metrics and check for anomalies
4. Strands Agent generates post-backtest narrative
"""

from __future__ import annotations

import logging
import math
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..execution.benchmarks import benchmark_for_strategy, build_60_40_equity
from ..execution.metrics import (
    bootstrap_sharpe_ci,
    build_equity_curve_from_trades,
    compute_deflated_sharpe,
    summarize_return_moments,
)
from ..execution.regimes import regime_comparison, vix_quartile_subwindows
from ..execution.walk_forward import (
    build_purged_walk_forward,
    filter_trades_in_fold_training,
    filter_trades_in_range,
    max_hold_days_from_trades,
)
from ..market_data_service import MarketDataService, OHLCVBar
from ..models import (
    BacktestConfig,
    BacktestRecord,
    BacktestResult,
    StrategyLabRecord,
    StrategySpec,
    TradeRecord,
    get_fee_defaults,
)
from ..signal_intelligence_models import SignalIntelligenceBriefV1
from ..trade_simulator import compute_metrics
from ..trading_service.modes.sandbox_compat import run_strategy_code
from .agents.alignment import TradeAlignmentAgent, TradeAlignmentReport
from .agents.analysis import AnalysisAgent
from .agents.ideation import IdeationAgent
from .agents.refinement import RefinementAgent
from .quality_gates.acceptance_gate import AcceptanceGate, summarize_acceptance_reason
from .quality_gates.backtest_anomaly import BacktestAnomalyDetector
from .quality_gates.code_safety import CodeSafetyChecker
from .quality_gates.convergence_tracker import ConvergenceTracker
from .quality_gates.models import QualityGateResult
from .quality_gates.strategy_validator import StrategySpecValidator

logger = logging.getLogger(__name__)

PhaseCallback = Callable[[str, Dict[str, Any]], None]

MAX_CODE_REFINEMENT_ROUNDS = 50
# Maximum number of trade-alignment problem-solving rounds. Each round
# audits the executed trades against the spec and, if misaligned, asks the
# alignment agent to rewrite the Python code; the new code is sent back
# through the sandbox for a fresh backtest. The cap prevents runaway loops
# when the agent cannot converge.
MAX_ALIGNMENT_ROUNDS = 10
# Legacy single-window acceptance threshold. Issue #247 replaced this with
# the composite ``AcceptanceGate`` (OOS DSR + IS→OOS degradation + OOS trade
# count + regime beats); ``WINNING_THRESHOLD`` is now only consulted as a
# fallback when ``BacktestConfig.walk_forward_enabled`` is False.
WINNING_THRESHOLD = 8.0


class StrategyLabOrchestrator:
    """Deterministic pipeline controller for the Strategy Lab.

    NOT a Strands Agent — the flow is fixed and quality gates must not be skippable.
    Strands Agents are used internally for LLM-powered steps (ideation, refinement,
    analysis).
    """

    def __init__(self, convergence_tracker: Optional[ConvergenceTracker] = None):
        self.ideation_agent = IdeationAgent()
        self.refinement_agent = RefinementAgent()
        self.alignment_agent = TradeAlignmentAgent()
        self.analysis_agent = AnalysisAgent()
        self.strategy_validator = StrategySpecValidator()
        self.code_safety_checker = CodeSafetyChecker()
        self.anomaly_detector = BacktestAnomalyDetector()
        self.acceptance_gate = AcceptanceGate()
        self.convergence_tracker = convergence_tracker or ConvergenceTracker()
        self.market_data_service = MarketDataService()

    def run_cycle(
        self,
        prior_records: List[StrategyLabRecord],
        config: BacktestConfig,
        signal_brief: Optional[SignalIntelligenceBriefV1] = None,
        on_phase: Optional[PhaseCallback] = None,
        exclude_asset_classes: Optional[List[str]] = None,
    ) -> StrategyLabRecord:
        """Run one full strategy lab cycle: ideate → code → backtest → analyze.

        Returns a StrategyLabRecord with the final result.
        """
        emit = on_phase or (lambda phase, data: None)

        # Gather convergence directives
        directives: List[str] = []
        stall_dir = self.convergence_tracker.get_stall_directive()
        if stall_dir:
            directives.append(stall_dir)
        diversity_dir = self.convergence_tracker.get_diversity_directive()
        if diversity_dir:
            directives.append(diversity_dir)
        directives.extend(self.convergence_tracker.get_failure_directives())

        # ── Phase 1: IDEATION ──────────────────────────────────────────
        emit("ideating", {"sub_phase": "started"})
        strategy_dict, code, rationale = self.ideation_agent.run(
            prior_records=prior_records,
            signal_brief=signal_brief,
            convergence_directives=directives or None,
            exclude_asset_classes=exclude_asset_classes,
        )

        # Build StrategySpec from ideation output
        strategy_id = f"strat-{uuid.uuid4().hex[:8]}"
        spec = StrategySpec(
            strategy_id=strategy_id,
            authored_by="strategy_lab_v2",
            asset_class=strategy_dict.get("asset_class", "stocks"),
            hypothesis=strategy_dict.get("hypothesis", ""),
            signal_definition=strategy_dict.get("signal_definition", ""),
            entry_rules=strategy_dict.get("entry_rules", []),
            exit_rules=strategy_dict.get("exit_rules", []),
            sizing_rules=strategy_dict.get("sizing_rules", []),
            risk_limits=strategy_dict.get("risk_limits", {}),
            speculative=strategy_dict.get("speculative", False),
            strategy_code=code,
        )

        # Override generic fee defaults with asset-class-appropriate values
        if config.transaction_cost_bps == 5.0 and config.slippage_bps == 2.0:
            fee_defaults = get_fee_defaults(spec.asset_class)
            config = config.model_copy(update=fee_defaults)

        emit(
            "ideating",
            {
                "sub_phase": "completed",
                "strategy": {
                    "asset_class": spec.asset_class,
                    "hypothesis": spec.hypothesis[:120],
                },
            },
        )

        all_gate_results: List[QualityGateResult] = []
        refinement_attempts: List[str] = []
        trades: List[TradeRecord] = []
        metrics = compute_metrics([], config.initial_capital, config.start_date, config.end_date)
        execution_succeeded = False
        market_data: Optional[Dict[str, List[OHLCVBar]]] = None

        # ── Phase 2: CODE REFINEMENT LOOP ─────────────────────────────
        # Iterate up to MAX_CODE_REFINEMENT_ROUNDS, refining the
        # generated code for correctness, performance, syntax errors,
        # build errors, runtime errors, and backtest anomalies (zero
        # trades, implausible returns, etc.).  The loop exits only when
        # all quality gates pass AND the backtest produces sound results.

        for round_num in range(MAX_CODE_REFINEMENT_ROUNDS):
            round_gate_results: List[QualityGateResult] = []

            # ── 2a: VALIDATE (spec + code safety) ────────────────────
            emit("coding", {"sub_phase": "started", "refinement_round": round_num})
            spec_gates = self.strategy_validator.validate(spec)
            code_gates = self.code_safety_checker.check(code)
            round_gate_results.extend(spec_gates)
            round_gate_results.extend(code_gates)
            for g in round_gate_results:
                g.refinement_round = round_num
            all_gate_results.extend(round_gate_results)

            checks_total = len(round_gate_results)
            checks_passed = sum(1 for g in round_gate_results if g.passed)

            critical_failures = [
                g for g in round_gate_results if not g.passed and g.severity == "critical"
            ]
            if critical_failures:
                emit(
                    "coding",
                    {
                        "sub_phase": "failed",
                        "refinement_round": round_num,
                        "checks_passed": checks_passed,
                        "checks_total": checks_total,
                    },
                )
                if round_num < MAX_CODE_REFINEMENT_ROUNDS - 1:
                    emit(
                        "coding",
                        {
                            "sub_phase": "refining",
                            "refinement_round": round_num,
                            "failure_phase": "validation",
                        },
                    )
                    failure_details = "\n".join(
                        f"- [{g.gate_name}] {g.details}" for g in critical_failures
                    )
                    updates, code = self._refine(
                        spec, code, "validation", failure_details, None, refinement_attempts
                    )
                    spec = self._apply_updates(spec, updates, code)
                    changes = updates.get("changes_made", "validation fix")
                    refinement_attempts.append(changes)
                    emit(
                        "coding",
                        {
                            "sub_phase": "refined",
                            "refinement_round": round_num,
                            "changes_made": changes,
                        },
                    )
                    continue
                else:
                    logger.warning(
                        "Max code refinement rounds reached on validation for %s", spec.strategy_id
                    )
                    break

            emit(
                "coding",
                {
                    "sub_phase": "completed",
                    "refinement_round": round_num,
                    "checks_passed": checks_passed,
                    "checks_total": checks_total,
                },
            )

            # ── 2b: FETCH DATA (once, reuse across rounds) ───────────
            if market_data is None:
                emit("backtesting", {"sub_phase": "fetching_data"})
                market_data = self._fetch_market_data(spec, config)
                if not market_data:
                    all_gate_results.append(
                        QualityGateResult(
                            gate_name="market_data",
                            passed=False,
                            severity="critical",
                            details=f"No market data available for asset class '{spec.asset_class}'.",
                            refinement_round=round_num,
                        )
                    )
                    break
                total_bars = sum(len(bars) for bars in market_data.values())
                emit(
                    "backtesting",
                    {
                        "sub_phase": "data_loaded",
                        "symbols_count": len(market_data),
                        "bars_count": total_bars,
                    },
                )

            # ── 2c: EXECUTE (syntax / runtime correctness) ───────────
            emit("backtesting", {"sub_phase": "running_code", "refinement_round": round_num})
            exec_result = run_strategy_code(code, market_data, config, strategy=spec)

            if not exec_result.success:
                all_gate_results.append(
                    QualityGateResult(
                        gate_name="code_execution",
                        passed=False,
                        severity="critical",
                        details=f"Execution failed ({exec_result.error_type}): {exec_result.stderr[:500]}",
                        refinement_round=round_num,
                    )
                )
                if round_num < MAX_CODE_REFINEMENT_ROUNDS - 1:
                    emit(
                        "coding",
                        {
                            "sub_phase": "refining",
                            "refinement_round": round_num,
                            "failure_phase": "execution",
                        },
                    )
                    failure_details = (
                        f"Error type: {exec_result.error_type}\n"
                        f"stderr:\n{exec_result.stderr[:2000]}"
                    )
                    updates, code = self._refine(
                        spec, code, "execution", failure_details, None, refinement_attempts
                    )
                    spec = self._apply_updates(spec, updates, code)
                    changes = updates.get("changes_made", "execution fix")
                    refinement_attempts.append(changes)
                    emit(
                        "coding",
                        {
                            "sub_phase": "refined",
                            "refinement_round": round_num,
                            "changes_made": changes,
                        },
                    )
                    continue
                else:
                    logger.warning(
                        "Max code refinement rounds reached on execution for %s", spec.strategy_id
                    )
                    break

            # ── 2d: COLLECT TRADES ────────────────────────────────────
            # TradingService has already finalised trades through
            # FillSimulator, so the legacy raw-trade validation step is a
            # no-op here. Kept the same ``trades`` variable name so the
            # rest of the loop is untouched.
            trades = exec_result.trades

            emit(
                "backtesting",
                {
                    "sub_phase": "completed",
                    "trades_count": len(trades),
                    "execution_time": exec_result.execution_time_seconds,
                },
            )

            # ── 2e: BACKTEST EVALUATION ───────────────────────────────
            # Code ran cleanly — now compute metrics and check for
            # anomalies.  Critical anomalies (zero trades, implausible
            # returns, etc.) trigger refinement while budget remains.
            metrics = compute_metrics(
                trades, config.initial_capital, config.start_date, config.end_date
            )

            anomaly_gates = self.anomaly_detector.check(
                metrics, trades, dsr_aware=config.walk_forward_enabled
            )
            for g in anomaly_gates:
                g.refinement_round = round_num
            all_gate_results.extend(anomaly_gates)

            critical_anomalies = [
                g for g in anomaly_gates if not g.passed and g.severity == "critical"
            ]
            if critical_anomalies:
                if round_num < MAX_CODE_REFINEMENT_ROUNDS - 1:
                    emit(
                        "coding",
                        {
                            "sub_phase": "refining",
                            "refinement_round": round_num,
                            "failure_phase": "evaluation",
                        },
                    )
                    failure_details = "\n".join(f"- {g.details}" for g in critical_anomalies)
                    updates, code = self._refine(
                        spec,
                        code,
                        "evaluation (backtest anomaly)",
                        failure_details,
                        metrics,
                        refinement_attempts,
                    )
                    spec = self._apply_updates(spec, updates, code)
                    changes = updates.get("changes_made", "anomaly fix")
                    refinement_attempts.append(changes)
                    emit(
                        "coding",
                        {
                            "sub_phase": "refined",
                            "refinement_round": round_num,
                            "changes_made": changes,
                        },
                    )
                    continue
                else:
                    logger.warning(
                        "Max code refinement rounds reached on evaluation for %s", spec.strategy_id
                    )
                    execution_succeeded = True  # anomalous but code is correct
                    break

            # All gates passed — code is clean and backtest is sound
            execution_succeeded = True
            break

        # ── Phase 2.5: TRADE ALIGNMENT LOOP ───────────────────────────
        # Now that the code runs cleanly and produces sensible aggregate
        # metrics, audit whether the executed trades actually implement
        # the strategy specification (entry/exit/sizing/risk rules). If
        # not, enter a problem-solving loop (capped at
        # MAX_ALIGNMENT_ROUNDS) where the alignment agent identifies the
        # bug, rewrites the Python code, and we send the script back
        # through the sandbox for a fresh backtest. The loop exits as
        # soon as the agent reports the trades are aligned (or the cap
        # is reached).
        alignment_attempts: List[str] = []
        alignment_reports: List[TradeAlignmentReport] = []

        if execution_succeeded and trades and market_data is not None:
            for align_round in range(MAX_ALIGNMENT_ROUNDS):
                emit(
                    "aligning",
                    {
                        "sub_phase": "evaluating",
                        "alignment_round": align_round,
                        "trades_count": len(trades),
                    },
                )

                report = self._run_alignment_audit(
                    spec=spec,
                    code=code,
                    trades=trades,
                    metrics=metrics,
                    prior_attempts=alignment_attempts,
                )
                alignment_reports.append(report)

                gate_severity = "info" if report.aligned else "critical"
                gate_details = (
                    report.rationale or "Trades aligned with strategy."
                    if report.aligned
                    else (
                        report.rationale
                        or f"Trades did not align with strategy ({len(report.issues)} issues)."
                    )
                )
                all_gate_results.append(
                    QualityGateResult(
                        gate_name="trade_alignment",
                        passed=report.aligned,
                        severity=gate_severity,  # type: ignore[arg-type]
                        details=gate_details,
                        refinement_round=align_round,
                    )
                )

                if report.aligned:
                    emit(
                        "aligning",
                        {
                            "sub_phase": "aligned",
                            "alignment_round": align_round,
                        },
                    )
                    break

                emit(
                    "aligning",
                    {
                        "sub_phase": "not_aligned",
                        "alignment_round": align_round,
                        "issues_count": len(report.issues),
                        "issues_preview": [
                            {
                                "rule_type": i.rule_type,
                                "severity": i.severity,
                                "description": i.description[:160],
                            }
                            for i in report.issues[:5]
                        ],
                    },
                )

                # Without a proposed code fix the loop has nothing to
                # send back to backtesting; stop early.
                if not report.proposed_code:
                    emit(
                        "aligning",
                        {
                            "sub_phase": "no_proposed_fix",
                            "alignment_round": align_round,
                        },
                    )
                    break

                if align_round >= MAX_ALIGNMENT_ROUNDS - 1:
                    emit(
                        "aligning",
                        {
                            "sub_phase": "max_rounds_reached",
                            "alignment_round": align_round,
                        },
                    )
                    logger.warning(
                        "Max alignment rounds (%d) reached for %s",
                        MAX_ALIGNMENT_ROUNDS,
                        spec.strategy_id,
                    )
                    break

                # The agent is confident enough to propose a rewrite.
                # Validate, re-execute in the sandbox, rebuild trades, and
                # re-run anomaly detection BEFORE committing the proposal
                # over the last known-good ``code`` / ``spec`` / ``trades``
                # / ``metrics``. If any of those checks fail, the loop
                # breaks with the prior backtest intact so the persisted
                # record never holds code that was never successfully
                # executed for the reported results.
                emit(
                    "aligning",
                    {
                        "sub_phase": "refining_code",
                        "alignment_round": align_round,
                        "predicted_aligned_after_fix": report.predicted_aligned_after_fix,
                    },
                )
                proposed_code = report.proposed_code
                proposed_spec = self._apply_updates(spec, {}, proposed_code)
                change_summary = report.changes_made or "alignment fix"

                # ── Re-validate code safety on the proposed code ──────
                safety_gates = self.code_safety_checker.check(proposed_code)
                for g in safety_gates:
                    g.refinement_round = align_round
                    g.gate_name = f"alignment_{g.gate_name}"
                all_gate_results.extend(safety_gates)
                critical_safety = [
                    g for g in safety_gates if not g.passed and g.severity == "critical"
                ]
                if critical_safety:
                    emit(
                        "aligning",
                        {
                            "sub_phase": "rejected_unsafe_code",
                            "alignment_round": align_round,
                            "details": "; ".join(g.details for g in critical_safety)[:400],
                        },
                    )
                    logger.warning(
                        "Alignment-proposed code failed safety gate for %s", spec.strategy_id
                    )
                    break

                # ── Send the script back to backtesting ───────────────
                emit(
                    "backtesting",
                    {
                        "sub_phase": "running_code",
                        "alignment_round": align_round,
                        "trigger": "trade_alignment_fix",
                    },
                )
                align_exec = run_strategy_code(proposed_code, market_data, config, strategy=spec)
                if not align_exec.success:
                    all_gate_results.append(
                        QualityGateResult(
                            gate_name="alignment_code_execution",
                            passed=False,
                            severity="critical",
                            details=(
                                f"Re-execution after alignment fix failed "
                                f"({align_exec.error_type}): {align_exec.stderr[:400]}"
                            ),
                            refinement_round=align_round,
                        )
                    )
                    emit(
                        "aligning",
                        {
                            "sub_phase": "re_execution_failed",
                            "alignment_round": align_round,
                            "error_type": align_exec.error_type,
                        },
                    )
                    break

                # Trades are already finalised by TradingService; the
                # legacy raw-trade validation step is a no-op here.
                new_trades = align_exec.trades

                new_metrics = compute_metrics(
                    new_trades, config.initial_capital, config.start_date, config.end_date
                )

                # ── Anomaly gates on the post-fix backtest ────────────
                # The main refinement loop runs these checks after every
                # sandbox round; the alignment loop must too, otherwise a
                # fix could introduce zero-trade or implausible-return
                # output that bypasses quality gates and still flows into
                # analysis and the win/loss classification.
                anomaly_gates = self.anomaly_detector.check(
                    new_metrics, new_trades, dsr_aware=config.walk_forward_enabled
                )
                for g in anomaly_gates:
                    g.refinement_round = align_round
                    g.gate_name = f"alignment_{g.gate_name}"
                all_gate_results.extend(anomaly_gates)
                critical_anomalies = [
                    g for g in anomaly_gates if not g.passed and g.severity == "critical"
                ]
                if critical_anomalies:
                    emit(
                        "aligning",
                        {
                            "sub_phase": "anomaly_detected",
                            "alignment_round": align_round,
                            "details": "; ".join(g.details for g in critical_anomalies)[:400],
                        },
                    )
                    logger.warning(
                        "Alignment fix introduced backtest anomaly for %s", spec.strategy_id
                    )
                    break

                # All gates passed — commit the proposal as the new
                # known-good state and continue to the next audit.
                code = proposed_code
                spec = proposed_spec
                trades = new_trades
                metrics = new_metrics
                alignment_attempts.append(change_summary)

                emit(
                    "aligning",
                    {
                        "sub_phase": "refined",
                        "alignment_round": align_round,
                        "changes_made": change_summary,
                        "trades_count": len(trades),
                    },
                )

        alignment_rounds = len(alignment_attempts)
        trades_aligned = bool(alignment_reports and alignment_reports[-1].aligned)

        # ── Phase 2.6: TRIAL COUNTING (issue #247) ────────────────────
        # Every refinement round on the same window contributes to the
        # multiple-testing burden the Deflated Sharpe Ratio corrects for.
        # Increment by ``len(refinement_attempts) + 1`` so the first
        # round (which has no recorded "attempt") still counts.
        self.convergence_tracker.increment_trials(max(1, len(refinement_attempts) + 1))

        # ── Phase 2.7: WALK-FORWARD + ACCEPTANCE GATE (issue #247) ────
        # Replaces the legacy ``WINNING_THRESHOLD`` annualized-return scalar
        # with a composite OOS gate evaluated on purged, embargoed K-fold
        # walk-forward diagnostics. Skipped when walk-forward is disabled
        # (legacy fallback) or there is no successful execution to evaluate.
        acceptance_results: List[QualityGateResult] = []
        acceptance_reason: Optional[str] = None
        walk_forward_failed = False
        if (
            execution_succeeded
            and trades
            and market_data is not None
            and config.walk_forward_enabled
        ):
            try:
                emit("backtesting", {"sub_phase": "walk_forward_started"})
                metrics = self._evaluate_walk_forward(spec, market_data, config, trades, metrics)
                acceptance_results = self.acceptance_gate.check(
                    metrics,
                    config,
                    n_trials=self.convergence_tracker.trial_count,
                )
                all_gate_results.extend(acceptance_results)
                acceptance_reason = summarize_acceptance_reason(acceptance_results)
                metrics = metrics.model_copy(
                    update={
                        "n_trials_when_accepted": self.convergence_tracker.trial_count,
                        "acceptance_reason": acceptance_reason,
                    }
                )
                emit(
                    "backtesting",
                    {
                        "sub_phase": "walk_forward_completed",
                        "deflated_sharpe": metrics.deflated_sharpe,
                        "oos_sharpe": metrics.oos_sharpe,
                        "is_oos_degradation_pct": metrics.is_oos_degradation_pct,
                        "oos_trade_count": metrics.oos_trade_count,
                        "n_trials": self.convergence_tracker.trial_count,
                        "acceptance_reason": acceptance_reason,
                    },
                )
            except Exception:
                logger.exception(
                    "Walk-forward evaluation failed for %s; falling back to "
                    "legacy single-window acceptance",
                    spec.strategy_id,
                )
                acceptance_results = []
                acceptance_reason = None
                walk_forward_failed = True

        # ── Resolve is_winning ────────────────────────────────────────
        # Walk-forward path: composite gate is authoritative.
        # Walk-forward fallback: anomaly checks during refinement ran with
        # ``dsr_aware=True``, which downgraded the ``Sharpe > 5.0`` flag
        # from critical to warning on the assumption that the OOS DSR
        # would adjudicate. With AcceptanceGate unavailable, re-run the
        # anomaly checks with ``dsr_aware=False`` and reject if any
        # critical fires — otherwise an obvious overfit could still be
        # marked winning on annualized return alone.
        # Legacy path (``walk_forward_enabled=False``): unchanged
        # ``WINNING_THRESHOLD`` comparison; the refinement loop already
        # ran with ``dsr_aware=False`` so no re-check is needed.
        if acceptance_results:
            is_winning = execution_succeeded and all(r.passed for r in acceptance_results)
        elif walk_forward_failed and execution_succeeded:
            fallback_anomalies = self.anomaly_detector.check(metrics, trades, dsr_aware=False)
            fallback_criticals = [
                g for g in fallback_anomalies if not g.passed and g.severity == "critical"
            ]
            is_winning = (
                metrics.annualized_return_pct > WINNING_THRESHOLD and not fallback_criticals
            )
            if fallback_criticals:
                # Surface the upgraded severities so the persisted
                # gate-result history reflects the true rejection reason.
                for g in fallback_anomalies:
                    g.gate_name = f"fallback_{g.gate_name}"
                all_gate_results.extend(fallback_anomalies)
        else:
            is_winning = execution_succeeded and metrics.annualized_return_pct > WINNING_THRESHOLD

        # ── Phase 3: ANALYSIS ─────────────────────────────────────────
        narrative = ""
        if execution_succeeded and trades:
            emit("analyzing", {"sub_phase": "draft"})
            try:

                def _on_analysis_sub(sub: str) -> None:
                    emit("analyzing", {"sub_phase": sub})

                narrative = self.analysis_agent.run(
                    spec, metrics, trades, rationale, on_sub_phase=_on_analysis_sub
                )
                emit("analyzing", {"sub_phase": "completed", "is_winning": is_winning})
            except Exception:
                logger.exception("Analysis agent failed for %s", spec.strategy_id)
                label = "winning" if is_winning else "losing"
                narrative = (
                    f"Auto-summary: {spec.asset_class} strategy ({label}) with "
                    f"annualized return {metrics.annualized_return_pct:.1f}%. "
                    f"(Detailed narrative generation failed.)"
                )
        elif not execution_succeeded:
            narrative = (
                f"Strategy failed to produce valid backtest results after "
                f"{len(refinement_attempts)} refinement round(s). "
                f"Last failure: {all_gate_results[-1].details if all_gate_results else 'unknown'}."
            )

        # ── Phase 4: RECORD ───────────────────────────────────────────
        now_iso = datetime.now(timezone.utc).isoformat()

        backtest_id = f"bt-{uuid.uuid4().hex[:8]}"
        backtest_record = BacktestRecord(
            backtest_id=backtest_id,
            strategy_id=spec.strategy_id,
            strategy=spec,
            config=config,
            submitted_by="strategy_lab_v2",
            submitted_at=now_iso,
            completed_at=now_iso,
            status="completed" if execution_succeeded else "failed",
            result=metrics,
            trades=trades,
        )

        lab_record_id = f"lab-{uuid.uuid4().hex[:8]}"
        record = StrategyLabRecord(
            lab_record_id=lab_record_id,
            strategy=spec,
            backtest=backtest_record,
            is_winning=is_winning,
            strategy_rationale=rationale,
            analysis_narrative=narrative,
            created_at=now_iso,
            refinement_rounds=len(refinement_attempts),
            quality_gate_results=[g.model_dump() for g in all_gate_results],
            strategy_code=code,
        )

        # Update convergence tracker
        self.convergence_tracker.record(spec, all_gate_results)

        emit(
            "complete",
            {
                "record_id": lab_record_id,
                "is_winning": is_winning,
                "metrics": metrics.model_dump(),
                "refinement_rounds": len(refinement_attempts),
                "alignment_rounds": alignment_rounds,
                "trades_aligned": trades_aligned,
            },
        )

        return record

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _refine(
        self,
        spec: StrategySpec,
        code: str,
        failure_phase: str,
        failure_details: str,
        metrics: Optional[BacktestResult],
        prior_attempts: List[str],
    ) -> tuple[Dict[str, Any], str]:
        """Call the refinement agent and return (updates_dict, new_code)."""
        try:
            return self.refinement_agent.run(
                spec=spec,
                code=code,
                failure_phase=failure_phase,
                failure_details=failure_details,
                metrics=metrics,
                prior_attempts=prior_attempts,
            )
        except Exception:
            logger.exception("Refinement agent failed, returning original code")
            return {"changes_made": "refinement failed — no changes"}, code

    def _run_alignment_audit(
        self,
        spec: StrategySpec,
        code: str,
        trades: List[TradeRecord],
        metrics: BacktestResult,
        prior_attempts: List[str],
    ) -> TradeAlignmentReport:
        """Call the alignment agent. Failures fall back to ``aligned=True`` so
        the orchestrator does not stall on a transient LLM error."""
        try:
            return self.alignment_agent.run(
                spec=spec,
                code=code,
                trades=trades,
                metrics=metrics,
                prior_attempts=prior_attempts,
            )
        except Exception as exc:
            logger.exception("Alignment agent raised; treating trades as aligned")
            return TradeAlignmentReport(
                aligned=True,
                rationale=(
                    "Alignment audit skipped: alignment agent raised "
                    f"{type(exc).__name__}. Treating trades as aligned to avoid stalling."
                ),
            )

    @staticmethod
    def _apply_updates(spec: StrategySpec, updates: Dict[str, Any], code: str) -> StrategySpec:
        """Apply refinement updates to the strategy spec."""
        data = spec.model_dump()
        for key in ("entry_rules", "exit_rules", "sizing_rules", "risk_limits", "hypothesis"):
            if key in updates:
                data[key] = updates[key]
        data["strategy_code"] = code
        return StrategySpec.model_validate(data)

    def _fetch_market_data(
        self,
        spec: StrategySpec,
        config: BacktestConfig,
    ) -> Optional[Dict[str, List[OHLCVBar]]]:
        """Fetch OHLCV data for the strategy's asset class.

        Issue #376 — when the strategy spec carries an
        ``audit.data_snapshot_id``, treat it as the ``as_of`` cutoff so
        a re-run of the saved spec replays the exact same snapshot.
        Specs without it use ``None`` (latest).
        """
        try:
            symbols = self.market_data_service.get_symbols_for_strategy(spec)
            if not symbols:
                return None
            as_of = (getattr(spec, "audit", None) and spec.audit.data_snapshot_id) or None
            data = self.market_data_service.fetch_multi_symbol_range(
                symbols=symbols[:5],
                asset_class=spec.asset_class,
                start_date=config.start_date,
                end_date=config.end_date,
                as_of=as_of,
            )
            return data if data else None
        except Exception:
            logger.exception("Market data fetch failed for %s", spec.asset_class)
            return None

    # ------------------------------------------------------------------
    # Issue #247 — walk-forward + acceptance-gate helpers
    # ------------------------------------------------------------------

    def _evaluate_walk_forward(
        self,
        spec: StrategySpec,
        market_data: Dict[str, List[OHLCVBar]],
        config: BacktestConfig,
        trades: List[TradeRecord],
        metrics: BacktestResult,
    ) -> BacktestResult:
        """Compute walk-forward IS/OOS diagnostics and populate the new
        ``BacktestResult`` fields the ``AcceptanceGate`` consumes.

        The strategy code is fixed for a cycle (no per-fold refit), so we
        partition the existing full-window trade ledger by ``exit_date`` into
        IS/OOS buckets per fold rather than re-running the strategy K times.
        Mathematically equivalent for OOS metrics and K× cheaper.
        """
        purge_hold_days = max_hold_days_from_trades(trades)
        embargo = config.embargo_days if config.embargo_days > 0 else purge_hold_days
        folds = build_purged_walk_forward(
            config.start_date,
            config.end_date,
            k_folds=config.n_folds,
            embargo_days=embargo,
            purge_hold_days=purge_hold_days,
        )

        fold_results: List[Dict[str, Any]] = []
        per_fold_oos_sharpe: List[float] = []
        per_fold_is_sharpe: List[float] = []
        oos_trade_count_total = 0
        all_oos_trades: List[TradeRecord] = []
        for fold in folds:
            oos_trades = filter_trades_in_range(trades, fold.test_start, fold.test_end)
            is_trades = filter_trades_in_fold_training(trades, fold)

            test_start_str = fold.test_start.isoformat()
            test_end_str = fold.test_end.isoformat()

            oos_metrics = compute_metrics(
                oos_trades, config.initial_capital, test_start_str, test_end_str
            )
            # IS Sharpe is computed per training segment (a fold may have up
            # to two disjoint segments — pre-test and post-test) and then
            # trade-count-weighted. Spanning the full backtest window would
            # include the test+purge+embargo gap as flat zero-return days
            # and dilute the Sharpe, materially understating IS→OOS
            # degradation.
            is_segment_sharpes: List[Tuple[float, int]] = []
            for tr in fold.train_ranges:
                seg_trades = filter_trades_in_range(is_trades, tr.start, tr.end)
                if not seg_trades:
                    continue
                seg_metrics = compute_metrics(
                    seg_trades,
                    config.initial_capital,
                    tr.start.isoformat(),
                    tr.end.isoformat(),
                )
                is_segment_sharpes.append((seg_metrics.sharpe_ratio, len(seg_trades)))

            if is_segment_sharpes:
                total_w = sum(w for _, w in is_segment_sharpes)
                fold_is_sharpe = (
                    sum(s * w for s, w in is_segment_sharpes) / total_w if total_w else 0.0
                )
            else:
                fold_is_sharpe = 0.0

            per_fold_oos_sharpe.append(oos_metrics.sharpe_ratio)
            if is_trades:
                per_fold_is_sharpe.append(fold_is_sharpe)
            oos_trade_count_total += len(oos_trades)
            all_oos_trades.extend(oos_trades)

            fold_results.append(
                {
                    "fold_index": fold.fold_index,
                    "test_start": test_start_str,
                    "test_end": test_end_str,
                    "oos_sharpe": oos_metrics.sharpe_ratio,
                    "is_sharpe": fold_is_sharpe,
                    "oos_trade_count": len(oos_trades),
                    "is_trade_count": len(is_trades),
                }
            )

        oos_sharpe = (
            sum(per_fold_oos_sharpe) / len(per_fold_oos_sharpe) if per_fold_oos_sharpe else 0.0
        )
        is_sharpe = sum(per_fold_is_sharpe) / len(per_fold_is_sharpe) if per_fold_is_sharpe else 0.0
        denom = max(abs(is_sharpe), 1e-9)
        is_oos_degradation_pct = max(0.0, 100.0 * (is_sharpe - oos_sharpe) / denom)

        # Pooled OOS daily-return series for DSR + bootstrap CI. Uses the
        # same equity-curve construction the metrics engine uses, so the
        # series is consistent with the per-fold OOS Sharpes.
        oos_returns = self._daily_returns_from_trades(
            all_oos_trades, config.initial_capital, config.start_date, config.end_date
        )
        skew, kurt = summarize_return_moments(oos_returns)
        deflated_sharpe = compute_deflated_sharpe(
            oos_sharpe,
            n_trials=self.convergence_tracker.trial_count,
            n_obs=len(oos_returns),
            skew=skew,
            kurtosis=kurt,
        )
        sharpe_ci_low, sharpe_ci_high = bootstrap_sharpe_ci(oos_returns, seed=0)

        regime_results = self._evaluate_regimes(spec, market_data, config, trades)

        return metrics.model_copy(
            update={
                "deflated_sharpe": round(deflated_sharpe, 4),
                "sharpe_ci_low": sharpe_ci_low,
                "sharpe_ci_high": sharpe_ci_high,
                "is_sharpe": round(is_sharpe, 4),
                "oos_sharpe": round(oos_sharpe, 4),
                "is_oos_degradation_pct": round(is_oos_degradation_pct, 2),
                "oos_trade_count": oos_trade_count_total,
                "regime_results": regime_results,
                "fold_results": fold_results,
            }
        )

    @staticmethod
    def _daily_returns_from_trades(
        trades: Sequence[TradeRecord],
        initial_capital: float,
        start_date: str,
        end_date: str,
    ) -> List[float]:
        """Daily log returns from the equity curve implied by the trades.

        Log basis matches :meth:`EquityCurve.daily_returns` and the rest of
        the metrics module, so OOS-Sharpe / DSR / bootstrap CIs computed
        downstream share the same return convention as the in-sample
        ``compute_performance_metrics`` Sharpe.

        If the equity curve crosses zero (portfolio ruin), the series is
        returned **empty** rather than zero-padding the ruin step. Zeroing
        a wipeout would convert it to a neutral day and let the OOS DSR /
        Sharpe CI / moments report misleadingly low risk; an empty series
        falls through every downstream consumer
        (:func:`summarize_return_moments`, :func:`compute_deflated_sharpe`,
        :func:`bootstrap_sharpe_ci`) as their well-defined "no data" path.
        """
        curve = build_equity_curve_from_trades(
            trades, initial_capital, start_date=start_date, end_date=end_date
        )
        if len(curve.equity) < 2:
            return []
        if any(v <= 0 for v in curve.equity):
            # Ruin: invalidate the whole series. Any downstream Sharpe / DSR
            # / CI on a curve that touched zero would be meaningless.
            return []
        out: List[float] = []
        for i in range(1, len(curve.equity)):
            out.append(math.log(curve.equity[i] / curve.equity[i - 1]))
        return out

    def _evaluate_regimes(
        self,
        spec: StrategySpec,
        market_data: Dict[str, List[OHLCVBar]],
        config: BacktestConfig,
        trades: List[TradeRecord],
    ) -> List[Dict[str, Any]]:
        """Per-regime strategy-vs-benchmark comparison for the acceptance gate.

        Builds a daily strategy return series from the trade ledger, a
        benchmark return series from the configured composition (defaults to
        a 60/40 SPY+AGG blend; falls back to a single-symbol benchmark when
        the blend cannot be assembled), aligns by length, then partitions
        into VIX-quartile sub-windows. Returns a list of four dicts shaped
        for ``AcceptanceGate``.
        """
        try:
            curve = build_equity_curve_from_trades(
                trades,
                config.initial_capital,
                start_date=config.start_date,
                end_date=config.end_date,
            )
            if len(curve.equity) < 2:
                return []
            strategy_returns = self._equity_to_returns(curve.equity)

            bench_dates, bench_equity = self._build_benchmark_equity(spec, market_data, config)
            if len(bench_equity) < 2:
                return []
            benchmark_returns = self._equity_to_returns(bench_equity)

            n = min(len(strategy_returns), len(benchmark_returns))
            strategy_returns = strategy_returns[:n]
            benchmark_returns = benchmark_returns[:n]
            aligned_dates = list(bench_dates[: n + 1])  # equity has n+1 points; returns has n

            subwindows = vix_quartile_subwindows(
                aligned_dates,
                benchmark_returns,
                vix_provider=self._resolve_vix_provider(),
            )
            return regime_comparison(strategy_returns, benchmark_returns, subwindows)
        except Exception:
            logger.exception("Regime evaluation failed for %s", spec.strategy_id)
            return []

    @staticmethod
    def _equity_to_returns(equity: Sequence[float]) -> List[float]:
        out: List[float] = []
        for i in range(1, len(equity)):
            prev = equity[i - 1]
            if prev <= 0:
                out.append(0.0)
            else:
                out.append((equity[i] - prev) / prev)
        return out

    def _build_benchmark_equity(
        self,
        spec: StrategySpec,
        market_data: Dict[str, List[OHLCVBar]],
        config: BacktestConfig,
    ) -> Tuple[List[Any], List[float]]:
        """Return ``(dates, equity)`` for the configured benchmark composition.

        ``benchmark_composition="60_40"`` blends SPY and AGG closes via
        :func:`build_60_40_equity`; any other value falls back to the
        asset-class default benchmark from :func:`benchmark_for_strategy`.
        Both paths normalize closes into an equity series scaled by
        ``config.initial_capital``.
        """
        composition = (config.benchmark_composition or "").strip().lower()
        # Issue #376 — pin benchmark fetches to the same ``as_of`` as the
        # strategy fetch so a saved spec re-runs against a consistent
        # historical snapshot of both strategy bars and benchmark bars.
        as_of = (getattr(spec, "audit", None) and spec.audit.data_snapshot_id) or None
        if composition == "60_40":
            try:
                blend = self.market_data_service.fetch_multi_symbol_range(
                    symbols=["SPY", "AGG"],
                    asset_class="stocks",
                    start_date=config.start_date,
                    end_date=config.end_date,
                    as_of=as_of,
                )
            except Exception:
                logger.exception("60/40 benchmark fetch failed; falling back to single-symbol")
                blend = None
            if blend and "SPY" in blend and "AGG" in blend and blend["SPY"] and blend["AGG"]:
                spy_bars = blend["SPY"]
                agg_bars = blend["AGG"]
                spy_dates = [self._parse_bar_date(b.date) for b in spy_bars]
                spy_equity = self._closes_to_equity(
                    [b.close for b in spy_bars], config.initial_capital
                )
                agg_equity = self._closes_to_equity(
                    [b.close for b in agg_bars], config.initial_capital
                )
                blended = build_60_40_equity(
                    spy_equity, agg_equity, initial_capital=config.initial_capital
                )
                n = min(len(spy_dates), len(blended))
                return spy_dates[:n], blended[:n]

        # Single-symbol fallback
        bench_symbol = benchmark_for_strategy(spec)
        try:
            single = self.market_data_service.fetch_multi_symbol_range(
                symbols=[bench_symbol],
                asset_class=spec.asset_class,
                start_date=config.start_date,
                end_date=config.end_date,
                as_of=as_of,
            )
        except Exception:
            logger.exception("Single-symbol benchmark fetch failed for %s", bench_symbol)
            single = None
        if single and bench_symbol in single and single[bench_symbol]:
            bars = single[bench_symbol]
            dates = [self._parse_bar_date(b.date) for b in bars]
            equity = self._closes_to_equity([b.close for b in bars], config.initial_capital)
            n = min(len(dates), len(equity))
            return dates[:n], equity[:n]
        return [], []

    @staticmethod
    def _closes_to_equity(closes: Sequence[float], initial_capital: float) -> List[float]:
        if not closes or closes[0] <= 0:
            return []
        scale = initial_capital / closes[0]
        return [c * scale for c in closes]

    @staticmethod
    def _parse_bar_date(d: str) -> Any:
        from datetime import date

        return date.fromisoformat(d[:10])

    @staticmethod
    def _resolve_vix_provider() -> Optional[Callable[[Sequence[Any]], List[float]]]:
        """Return a VIX provider callable when ``STRATEGY_LAB_VIX_SOURCE`` is
        set, otherwise None so :func:`vix_quartile_subwindows` falls back to
        realized-vol on the benchmark series. Production deployments can
        wire in a Yahoo ``^VIX`` fetcher here without touching callers."""
        source = os.environ.get("STRATEGY_LAB_VIX_SOURCE", "").strip().lower()
        if not source:
            return None
        # Hook point for production providers; unset → realized-vol fallback.
        return None
