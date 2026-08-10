"""AlignmentMixin — the trade-alignment audit/fix loop cluster extracted
from :mod:`orchestrator`.

Pure move (issue #2585, part of #2571 decomposing the StrategyLabOrchestrator
god-class tracking issue): every method and helper below is relocated
verbatim from ``orchestrator.py``. No behavior changes, with one narrow
exception — ``_evaluate_alignment_proposal`` resolves ``compute_metrics``
through a function-local ``from . import orchestrator as _orchestrator_module``
deferred import instead of a static module-level import. This is required
(not a stylistic choice): ``test_acceptance_gate_integration.py`` /
``_walk_forward_test_helpers.py`` monkeypatch
``investment_team.strategy_lab.orchestrator.compute_metrics`` directly, and a
static import here would bind a private reference in this module's globals
that such a patch would never reach. Do not "clean up" this deferred import
back into a static one — see the identical, pre-existing idiom in
``orchestrator_synthesis.py``'s ``_cached_run_strategy_code`` /
``_run_synthesis_loop`` / ``_evaluate_synthesis_round`` for precedent.
``AlignmentMixin`` is mixed into ``StrategyLabOrchestrator`` — see the class
statement in ``orchestrator.py`` for the current base order (more mixins
have since joined it); its methods expect the attributes
``StrategyLabOrchestrator.__init__`` sets on ``self``
(``self.code_safety_checker``), plus the ``self.record_gates`` /
``self.build_orchestrator_gate`` / ``self._apply_updates`` /
``self._cached_run_strategy_code`` / ``self._check_anomalies_cached`` /
``self._run_alignment_audit`` / ``self._committed_code_conformance_verdict``
methods — all of which stay on the base class and resolve via MRO on the
final composed instance.

This module must not import anything from ``orchestrator.py`` at module level
(that would be circular: ``orchestrator.py`` imports ``AlignmentMixin`` from
here before its own class statement executes) — the deferred import described
above is the sole, intentional exception. Pure helpers shared by both this
cluster and code that stays in ``orchestrator.py`` live in
``_orchestrator_helpers.py`` instead.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..market_data_service import OHLCVBar
from ..models import BacktestConfig, BacktestResult, StrategySpec, TradeRecord
from ..trading_service.modes.sandbox_compat import StrategyRunResult
from ._orchestrator_helpers import (
    _AlignmentLoopOutcome,
    _attach_execution_diagnostics,
    _DriftCollector,
    _format_execution_diagnostics,
    _maybe_attach_coverage_report,
)
from .agents._llm_budget import DesignBudgetExhausted
from .agents.alignment import TradeAlignmentReport
from .budget_config import StrategyLabBudgetConfig
from .exceptions import OrchestratorContractError, SpecImplementabilityError
from .quality_gates.models import QualityGateResult, join_gate_details

logger = logging.getLogger(__name__)

PhaseCallback = Callable[[str, Dict[str, Any]], None]

# Maximum number of trade-alignment problem-solving rounds. Each round
# audits the executed trades against the spec and, if misaligned, asks the
# alignment agent to rewrite the Python code; the new code is sent back
# through the sandbox for a fresh backtest. The cap prevents runaway loops
# when the agent cannot converge.
MAX_ALIGNMENT_ROUNDS = StrategyLabBudgetConfig.from_env().max_alignment_rounds


@dataclass
class _AlignmentRoundOutcome:
    """One iteration of ``_run_trade_alignment_loop``.

    Semantics:
    - ``terminate=True`` ⇒ caller breaks the loop. The spec/code/trades/
      metrics fields carry the pre-iteration state (either because the
      audit reported aligned, the proposal was rejected, or the round
      budget is spent).
    - ``terminate=False`` ⇒ caller continues. The spec/code/trades/
      metrics fields carry the just-committed proposal as the new
      known-good state.

    The helper mutates ``alignment_reports``, ``alignment_attempts``,
    and ``all_gate_results`` in place; callers observe those lists
    directly.
    """

    spec: StrategySpec
    code: str
    trades: List[TradeRecord]
    metrics: BacktestResult
    terminate: bool
    # Set on a committing round (``terminate=False``) to the conformance
    # verdict of the just-committed code; ignored on terminate rounds (which
    # carry the unchanged pre-iteration state).
    ran_on_non_conforming_code: bool = False


class AlignmentMixin:
    """Trade-alignment audit/fix loop mixed into ``StrategyLabOrchestrator``."""

    def _run_trade_alignment_loop(
        self,
        *,
        spec: StrategySpec,
        code: str,
        trades: List[TradeRecord],
        metrics: BacktestResult,
        market_data: Dict[str, List[OHLCVBar]],
        config: BacktestConfig,
        execution_succeeded: bool,
        all_gate_results: List[QualityGateResult],
        emit: PhaseCallback,
        ran_on_non_conforming_code: bool = False,
        drift_collector: Optional[_DriftCollector] = None,
    ) -> _AlignmentLoopOutcome:
        """Run the trade-alignment audit loop after the synthesis loop settles.

        Pre: synthesis loop has produced (``code``, ``spec``, ``trades``,
        ``metrics``) plus ``market_data`` was fetched at least once and
        ``execution_succeeded`` tracks whether the last execution cleared
        the anomaly gates. ``ran_on_non_conforming_code`` is the synthesis
        loop's verdict for the incoming ``trades``.
        Post: returns an ``_AlignmentLoopOutcome`` carrying the (possibly
        updated) ``spec`` / ``code`` / ``trades`` / ``metrics`` plus the
        attempt-string history and per-round reports the caller persists.
        ``ran_on_non_conforming_code`` is carried through unchanged when no
        round commits, and re-derived from each committed round's code (which
        replaces the persisted trades) so it always describes the returned
        ``trades``.
        Mutates ``all_gate_results`` in place (gates appended with
        ``alignment_`` prefix on each commit / failure).

        The loop exits early when:
          * The agent reports the trades aligned (``aligned=True``) — committed.
          * The agent returns no ``proposed_code`` — nothing to retry.
          * ``MAX_ALIGNMENT_ROUNDS`` is reached.
          * The proposed code fails code-safety or sandbox re-execution.
          * The post-fix backtest trips a critical anomaly.
        Every break short-circuits the loop with the known-good state from
        the most recent committed round.
        """
        alignment_attempts: List[str] = []
        alignment_reports: List[TradeAlignmentReport] = []

        # Issue #527 — engine-side enforcement of structured ``exit_rules``
        # has a deterministic conformance gate that runs once the trade
        # ledger is settled. It runs AFTER this loop because alignment
        # re-execution can replace ``trades`` / ``metrics`` with a new
        # ledger that has different conformance characteristics.
        if not (execution_succeeded and trades and market_data is not None):
            return _AlignmentLoopOutcome(
                spec=spec,
                code=code,
                trades=trades,
                metrics=metrics,
                alignment_attempts=alignment_attempts,
                alignment_reports=alignment_reports,
                trades_aligned=False,
                ran_on_non_conforming_code=ran_on_non_conforming_code,
            )

        for align_round in range(MAX_ALIGNMENT_ROUNDS):
            round_outcome = self._run_alignment_round(
                spec=spec,
                code=code,
                trades=trades,
                metrics=metrics,
                market_data=market_data,
                config=config,
                align_round=align_round,
                all_gate_results=all_gate_results,
                alignment_attempts=alignment_attempts,
                alignment_reports=alignment_reports,
                emit=emit,
                drift_collector=drift_collector,
            )
            spec, code = round_outcome.spec, round_outcome.code
            trades, metrics = round_outcome.trades, round_outcome.metrics
            # A committing round (``terminate=False``) replaced the persisted
            # trades with its proposed code; adopt that code's conformance
            # verdict. Terminate rounds carry the unchanged prior state, so the
            # flag is left as-is.
            if not round_outcome.terminate:
                ran_on_non_conforming_code = round_outcome.ran_on_non_conforming_code
            if round_outcome.terminate:
                break

        has_reports = bool(alignment_reports)
        last_report = alignment_reports[-1] if has_reports else None

        unresolved_criticals: List[Any] = []
        if last_report and last_report.alignment_findings:
            unresolved_criticals = [
                f
                for f in last_report.alignment_findings
                if not f.passed and f.severity == "critical"
            ]

        last_aligned = has_reports and last_report.aligned  # type: ignore[union-attr]
        if unresolved_criticals and last_aligned:
            logger.warning(
                "Alignment report claims aligned but %d critical findings "
                "remain unresolved for %s; overriding trades_aligned=False",
                len(unresolved_criticals),
                spec.strategy_id,
            )
            last_report.aligned = False  # type: ignore[union-attr]
            last_report.rationale = (  # type: ignore[union-attr]
                f"Override: deterministic gate found {len(unresolved_criticals)} "
                "unresolved critical finding(s) despite report claiming aligned"
            )
            for gate in reversed(all_gate_results):
                if gate.gate_name == "trade_alignment":
                    gate.passed = False
                    gate.severity = "critical"
                    gate.details = last_report.rationale  # type: ignore[union-attr]
                    break

        trades_aligned_final = last_aligned and not unresolved_criticals
        rejection_reason: Optional[str] = None
        if has_reports and not trades_aligned_final:
            rejection_reason = "alignment_unresolved"

        return _AlignmentLoopOutcome(
            spec=spec,
            code=code,
            trades=trades,
            metrics=metrics,
            alignment_attempts=alignment_attempts,
            alignment_reports=alignment_reports,
            trades_aligned=trades_aligned_final,
            rejection_reason=rejection_reason,
            ran_on_non_conforming_code=ran_on_non_conforming_code,
        )

    def _run_alignment_round(
        self,
        *,
        spec: StrategySpec,
        code: str,
        trades: List[TradeRecord],
        metrics: BacktestResult,
        market_data: Dict[str, List[OHLCVBar]],
        config: BacktestConfig,
        align_round: int,
        all_gate_results: List[QualityGateResult],
        alignment_attempts: List[str],
        alignment_reports: List[TradeAlignmentReport],
        emit: PhaseCallback,
        drift_collector: Optional[_DriftCollector] = None,
    ) -> _AlignmentRoundOutcome:
        """One iteration of ``_run_trade_alignment_loop``.

        Pre: ``align_round`` is the current 0-indexed iteration;
        ``alignment_reports``, ``alignment_attempts``, ``all_gate_results``
        are running lists the helper mutates in place.
        Post: returns an ``_AlignmentRoundOutcome``. On ``terminate=True``
        the caller breaks (state carries the pre-iteration values);
        on ``terminate=False`` the caller continues (state carries the
        committed proposal as the new known-good baseline).

        Step sequence:
          1. Audit current trades → append report → record gate.
          2. If aligned: terminate with success (no state change).
          3. If no proposed fix: terminate.
          4. If at max rounds: terminate.
          5. Run code-safety on proposed code; if critical: terminate.
          6. Re-execute proposed code; if failed: terminate.
          7. Compute metrics + coverage; run anomaly gates;
             if critical: terminate.
          8. Commit proposal as new known-good state; continue.
        """
        if align_round < 0:
            raise OrchestratorContractError(f"align_round must be non-negative, got {align_round}")
        if not isinstance(market_data, dict) or not market_data:
            raise OrchestratorContractError("market_data must be non-empty")

        # Step 1 — audit the current ledger and record the alignment gates.
        report = self._audit_and_record_alignment(
            spec=spec,
            code=code,
            trades=trades,
            metrics=metrics,
            market_data=market_data,
            config=config,
            align_round=align_round,
            all_gate_results=all_gate_results,
            alignment_attempts=alignment_attempts,
            alignment_reports=alignment_reports,
            emit=emit,
        )

        # Every early exit returns the pre-iteration state so the caller breaks
        # the loop on the last known-good baseline.
        def _terminate() -> _AlignmentRoundOutcome:
            return _AlignmentRoundOutcome(
                spec=spec, code=code, trades=trades, metrics=metrics, terminate=True
            )

        # Step 2 — already aligned / no proposed fix / out of rounds all stop.
        if not self._alignment_proposal_eligible(
            report=report, align_round=align_round, spec=spec, emit=emit
        ):
            return _terminate()

        # Step 3 — apply the proposed fix, re-validate safety, re-execute.
        proposed = self._validate_and_reexecute_alignment_proposal(
            spec=spec,
            report=report,
            market_data=market_data,
            config=config,
            all_gate_results=all_gate_results,
            align_round=align_round,
            drift_collector=drift_collector,
            emit=emit,
        )
        if proposed is None:
            return _terminate()
        proposed_spec, proposed_code, align_exec = proposed

        # Step 4 — recompute metrics and re-run the anomaly gates on the fix.
        evaluated = self._evaluate_alignment_proposal(
            proposed_spec=proposed_spec,
            align_exec=align_exec,
            market_data=market_data,
            config=config,
            all_gate_results=all_gate_results,
            align_round=align_round,
            spec=spec,
            emit=emit,
        )
        if evaluated is None:
            return _terminate()
        new_trades, new_metrics = evaluated

        # Step 5 — commit the proposal as the new known-good baseline.
        return self._commit_alignment_proposal(
            spec=spec,
            code=code,
            proposed_spec=proposed_spec,
            proposed_code=proposed_code,
            new_trades=new_trades,
            new_metrics=new_metrics,
            change_summary=report.changes_made or "alignment fix",
            alignment_attempts=alignment_attempts,
            all_gate_results=all_gate_results,
            align_round=align_round,
            drift_collector=drift_collector,
            emit=emit,
        )

    def _audit_and_record_alignment(
        self,
        *,
        spec: StrategySpec,
        code: str,
        trades: List[TradeRecord],
        metrics: BacktestResult,
        market_data: Dict[str, List[OHLCVBar]],
        config: BacktestConfig,
        align_round: int,
        all_gate_results: List[QualityGateResult],
        alignment_attempts: List[str],
        alignment_reports: List[TradeAlignmentReport],
        emit: PhaseCallback,
    ) -> TradeAlignmentReport:
        """Audit the current trade ledger and record the alignment gates.

        Pre: ``trades`` is the ledger to audit; ``alignment_reports`` and
        ``all_gate_results`` are running lists.
        Post: appends the fresh report to ``alignment_reports`` and the
        per-rule + aggregate ``trade_alignment`` gate rows (stamped with
        ``align_round``) to ``all_gate_results``, both in place; returns the
        report for the caller's eligibility / proposal decisions.
        """
        emit(
            "aligning",
            {
                "sub_phase": "evaluating",
                "alignment_round": align_round,
                "trades_count": len(trades),
            },
        )
        try:
            report, gate_results = self._run_alignment_audit(
                spec=spec,
                code=code,
                trades=trades,
                metrics=metrics,
                prior_attempts=alignment_attempts,
                market_data=market_data,
                config=config,
            )
        except DesignBudgetExhausted as exc:
            exc.latest_spec = spec
            exc.latest_code = code
            raise
        alignment_reports.append(report)

        # Per-rule gate rows from the deterministic checker. Stamp the
        # round number so the dashboard renders them under the right
        # alignment-iteration column.
        for g in gate_results:
            g.refinement_round = align_round
        all_gate_results.extend(gate_results)

        # Aggregate gate row for the existing "trade_alignment" roll-up
        # — same shape as before so downstream consumers that only look
        # at the single row don't break.
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
            self.build_orchestrator_gate(
                "trade_alignment",
                phase="verification",
                severity=gate_severity,  # type: ignore[arg-type]
                details=gate_details,
                refinement_round=align_round,
            )
        )
        return report

    def _alignment_proposal_eligible(
        self,
        *,
        report: TradeAlignmentReport,
        align_round: int,
        spec: StrategySpec,
        emit: PhaseCallback,
    ) -> bool:
        """Decide whether the audited report yields a fix worth re-executing.

        Pre: ``report`` is the freshly audited alignment report.
        Post: returns ``True`` only when the trades are not yet aligned, a
        ``proposed_code`` fix exists, and the round budget is not exhausted —
        i.e. the caller should proceed to validate/re-execute the proposal.
        Returns ``False`` on the three terminal cases (already aligned, no
        proposed fix, max rounds reached), emitting the matching ``aligning``
        sub-phase event for each. Pure aside from the emitted telemetry.
        """
        if report.aligned:
            emit("aligning", {"sub_phase": "aligned", "alignment_round": align_round})
            return False

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
                        "description": i.description,
                    }
                    for i in report.issues[:5]
                ],
                # Per-rule deterministic findings preview. The full
                # ledger lives on the persisted ``BacktestRecord``;
                # surface only the first 10 here so the SSE payload
                # stays bounded.
                "findings_preview": [
                    {
                        "trade_num": f.trade_num,
                        "check_name": f.check_name,
                        "rule_id": f.rule_id,
                        "severity": f.severity,
                        "passed": f.passed,
                        "details": f.details,
                    }
                    for f in report.alignment_findings
                ],
                "findings_count": len(report.alignment_findings),
            },
        )

        if not report.proposed_code:
            emit(
                "aligning",
                {"sub_phase": "no_proposed_fix", "alignment_round": align_round},
            )
            return False

        if align_round >= MAX_ALIGNMENT_ROUNDS - 1:
            emit(
                "aligning",
                {"sub_phase": "max_rounds_reached", "alignment_round": align_round},
            )
            logger.warning(
                "Max alignment rounds (%d) reached for %s",
                MAX_ALIGNMENT_ROUNDS,
                spec.strategy_id,
            )
            return False

        return True

    def _validate_and_reexecute_alignment_proposal(
        self,
        *,
        spec: StrategySpec,
        report: TradeAlignmentReport,
        market_data: Dict[str, List[OHLCVBar]],
        config: BacktestConfig,
        all_gate_results: List[QualityGateResult],
        align_round: int,
        drift_collector: Optional[_DriftCollector],
        emit: PhaseCallback,
    ) -> Optional[Tuple[StrategySpec, str, StrategyRunResult]]:
        """Validate the proposed fix's safety and re-execute it.

        Pre: ``report.proposed_code`` is non-empty (eligibility already
        confirmed); ``all_gate_results`` is the running gate list.
        Post: returns ``(proposed_spec, proposed_code, align_exec)`` when the
        proposed code passes the safety gate and re-executes successfully.
        Returns ``None`` to signal the caller should terminate the round when a
        critical safety finding fires or the re-execution fails — recording the
        ``alignment_``-prefixed safety gates / execution gate and emitting the
        matching sub-phase. Records the safety gates on ``all_gate_results`` in
        place.
        Raises: ``SpecImplementabilityError`` (with ``drift_collector``
        attached) when the proposed code cannot be applied to the spec.
        """
        emit(
            "aligning",
            {
                "sub_phase": "refining_code",
                "alignment_round": align_round,
                "predicted_aligned_after_fix": report.predicted_aligned_after_fix,
            },
        )
        proposed_code = report.proposed_code
        try:
            proposed_spec = self._apply_updates(spec, {}, proposed_code)
        except SpecImplementabilityError as exc:
            exc.drift_collector = drift_collector
            raise

        # Re-validate code safety on the proposed code — alignment runs
        # after backtest, so the phase tag is verification.
        safety_gates = self.code_safety_checker.check(
            proposed_code, proposed_spec, phase="verification"
        )
        self.record_gates(
            safety_gates,
            all_gate_results,
            refinement_round=align_round,
            gate_name_prefix="alignment_",
        )
        critical_safety = [g for g in safety_gates if not g.passed and g.severity == "critical"]
        if critical_safety:
            emit(
                "aligning",
                {
                    "sub_phase": "rejected_unsafe_code",
                    "alignment_round": align_round,
                    "details": join_gate_details(critical_safety),
                },
            )
            logger.warning("Alignment-proposed code failed safety gate for %s", spec.strategy_id)
            return None

        emit(
            "backtesting",
            {
                "sub_phase": "running_code",
                "alignment_round": align_round,
                "trigger": "trade_alignment_fix",
            },
        )
        align_exec = self._cached_run_strategy_code(
            proposed_code, market_data, config, strategy=spec
        )
        if not align_exec.success:
            all_gate_results.append(
                self.build_orchestrator_gate(
                    "alignment_code_execution",
                    phase="verification",
                    details=(
                        f"Re-execution after alignment fix failed "
                        f"({align_exec.error_type}): {align_exec.stderr}"
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
            return None

        return proposed_spec, proposed_code, align_exec

    def _evaluate_alignment_proposal(
        self,
        *,
        proposed_spec: StrategySpec,
        align_exec: StrategyRunResult,
        market_data: Dict[str, List[OHLCVBar]],
        config: BacktestConfig,
        all_gate_results: List[QualityGateResult],
        align_round: int,
        spec: StrategySpec,
        emit: PhaseCallback,
    ) -> Optional[Tuple[List[TradeRecord], BacktestResult]]:
        """Recompute metrics for the re-executed fix and re-run anomaly gates.

        Pre: ``align_exec`` is a successful re-execution of the proposed code;
        ``proposed_spec`` carries that code (used for the coverage probe).
        Post: returns ``(new_trades, new_metrics)`` — metrics carrying this
        re-execution's diagnostics / coverage — when no critical anomaly fires.
        Returns ``None`` to signal the caller should terminate when a critical
        anomaly is detected, emitting ``anomaly_detected`` and logging the
        diagnostics. Records the ``alignment_``-prefixed anomaly gates on
        ``all_gate_results`` in place.
        """
        # Local import — same deferred-import rationale as
        # ``SynthesisMixin._cached_run_strategy_code``: keeps test
        # monkeypatches of ``orchestrator.compute_metrics`` honored.
        from . import orchestrator as _orchestrator_module

        new_trades = align_exec.trades
        new_metrics = _orchestrator_module.compute_metrics(
            new_trades, config.initial_capital, config.start_date, config.end_date
        )
        # Carry this re-execution's engine exit-rule firing counters onto the
        # committed metrics so the verification-phase conformance gate sees the
        # firings that match ``new_trades`` (not the ``None`` default).
        _attach_execution_diagnostics(metrics=new_metrics, exec_result=align_exec)

        # Alignment re-backtest path attaches a CoverageReport when the
        # fix produced zero/low trades. Pass ``proposed_spec`` (which
        # carries ``proposed_code``) — the loop-level ``spec`` still
        # holds the pre-alignment source.
        _maybe_attach_coverage_report(
            metrics=new_metrics,
            spec=proposed_spec,
            market_data=market_data,
            config=config,
            exec_result=align_exec,
        )

        anomaly_gates = self._check_anomalies_cached(
            new_metrics,
            new_trades,
            dsr_aware=config.walk_forward_enabled,
            diagnostics=align_exec.execution_diagnostics,
            coverage_report=new_metrics.coverage_report,
            phase="verification",
            market_data=market_data,
        )
        self.record_gates(
            anomaly_gates,
            all_gate_results,
            refinement_round=align_round,
            gate_name_prefix="alignment_",
        )
        critical_anomalies = [g for g in anomaly_gates if not g.passed and g.severity == "critical"]
        if critical_anomalies:
            diagnostics_block = _format_execution_diagnostics(align_exec.execution_diagnostics)
            emit_payload: Dict[str, Any] = {
                "sub_phase": "anomaly_detected",
                "alignment_round": align_round,
                "details": join_gate_details(critical_anomalies),
            }
            if diagnostics_block:
                emit_payload["execution_diagnostics"] = diagnostics_block
            emit("aligning", emit_payload)
            if diagnostics_block:
                logger.warning(
                    "Alignment fix introduced backtest anomaly for %s — %s",
                    spec.strategy_id,
                    diagnostics_block,
                )
            else:
                logger.warning(
                    "Alignment fix introduced backtest anomaly for %s",
                    spec.strategy_id,
                )
            return None

        return new_trades, new_metrics

    def _commit_alignment_proposal(
        self,
        *,
        spec: StrategySpec,
        code: str,
        proposed_spec: StrategySpec,
        proposed_code: str,
        new_trades: List[TradeRecord],
        new_metrics: BacktestResult,
        change_summary: str,
        alignment_attempts: List[str],
        all_gate_results: List[QualityGateResult],
        align_round: int,
        drift_collector: Optional[_DriftCollector],
        emit: PhaseCallback,
    ) -> _AlignmentRoundOutcome:
        """Commit the validated proposal as the new known-good baseline.

        Pre: the proposed fix passed safety, re-execution, and the anomaly
        gates; ``alignment_attempts`` is the running change-log list.
        Post: re-checks predicate conformance on the committed code (recording
        the verdict gate), appends ``change_summary`` to ``alignment_attempts``,
        records the code/spec drift, emits ``refined``, and returns a
        non-terminating ``_AlignmentRoundOutcome`` carrying the proposal as the
        new baseline plus its ``ran_on_non_conforming_code`` flag.
        """
        # The committed proposal becomes the persisted backtest; re-check
        # predicate conformance on it so the non-conforming flag tracks the
        # committed code (the alignment path does not otherwise re-run the gate).
        committed_non_conforming = self._committed_code_conformance_verdict(
            proposed_code,
            proposed_spec,
            all_gate_results=all_gate_results,
            refinement_round=align_round,
            gate_name_prefix="alignment_",
        )

        # All gates passed — commit the proposal as the new known-good state.
        alignment_attempts.append(change_summary)
        if drift_collector is not None:
            drift_collector.record_code_change(
                phase="verification",
                agent="TradeAlignmentAgent",
                before_code=code,
                after_code=proposed_code,
                reason=change_summary,
            )
            drift_collector.record_spec_change(
                phase="verification",
                agent="TradeAlignmentAgent",
                before_spec=spec,
                after_spec=proposed_spec,
                reason=change_summary,
            )
        emit(
            "aligning",
            {
                "sub_phase": "refined",
                "alignment_round": align_round,
                "changes_made": change_summary,
                "trades_count": len(new_trades),
            },
        )
        return _AlignmentRoundOutcome(
            spec=proposed_spec,
            code=proposed_code,
            trades=new_trades,
            metrics=new_metrics,
            terminate=False,
            ran_on_non_conforming_code=committed_non_conforming,
        )
