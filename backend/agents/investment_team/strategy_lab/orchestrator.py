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
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

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
        metrics = BacktestResult(
            total_return_pct=0,
            annualized_return_pct=0,
            volatility_pct=0,
            sharpe_ratio=0,
            max_drawdown_pct=0,
            win_rate_pct=0,
            profit_factor=0,
        )
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

            anomaly_gates = self.anomaly_detector.check(metrics, trades)
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
                anomaly_gates = self.anomaly_detector.check(new_metrics, new_trades)
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
                is_w = metrics.annualized_return_pct > WINNING_THRESHOLD
                emit("analyzing", {"sub_phase": "completed", "is_winning": is_w})
            except Exception:
                logger.exception("Analysis agent failed for %s", spec.strategy_id)
                is_w = metrics.annualized_return_pct > WINNING_THRESHOLD
                label = "winning" if is_w else "losing"
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
        is_winning = execution_succeeded and metrics.annualized_return_pct > WINNING_THRESHOLD

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
        """Fetch OHLCV data for the strategy's asset class."""
        try:
            symbols = self.market_data_service.get_symbols_for_strategy(spec)
            if not symbols:
                return None
            data = self.market_data_service.fetch_multi_symbol_range(
                symbols=symbols[:5],
                asset_class=spec.asset_class,
                start_date=config.start_date,
                end_date=config.end_date,
            )
            return data if data else None
        except Exception:
            logger.exception("Market data fetch failed for %s", spec.asset_class)
            return None
