"""VerificationMixin — the verification/publication-decision cluster
extracted from :mod:`orchestrator`.

Pure move: every method below is relocated verbatim from ``orchestrator.py``.
No behavior changes. ``VerificationMixin`` is mixed into
``StrategyLabOrchestrator`` (see the class statement in ``orchestrator.py``
for the current base order); its methods expect the attributes
``StrategyLabOrchestrator.__init__`` sets on
``self`` (``self.acceptance_gate``, ``self.convergence_tracker``,
``self.anomaly_detector``), plus the ``self.record_gates`` /
``self._evaluate_walk_forward`` / ``self._run_realism_gates`` methods — all of
which stay on the base class (or a sibling mixin) and resolve via MRO on the
final composed instance.

This module must not import anything from ``orchestrator.py`` at module level
(that would be circular: ``orchestrator.py`` imports ``VerificationMixin``
from here before its own class statement executes). Pure helpers shared by
both this cluster and code that stays in ``orchestrator.py`` live in
``_orchestrator_helpers.py`` instead.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..market_data_service import OHLCVBar
from ..models import WINNING_THRESHOLD, BacktestConfig, BacktestResult, StrategySpec, TradeRecord
from ._orchestrator_helpers import (
    _apply_veto_to_acceptance_reason,
    _VerificationOutcome,
    publishability_skip_reason,
)
from .agents.alignment import TradeAlignmentReport
from .quality_gates.acceptance_gate import summarize_acceptance_reason
from .quality_gates.exit_rule_conformance import ExitRuleConformanceGate
from .quality_gates.models import QualityGateResult, join_gate_details

logger = logging.getLogger(__name__)

PhaseCallback = Callable[[str, Dict[str, Any]], None]


class VerificationMixin:
    """Verification/publication-veto cluster mixed into ``StrategyLabOrchestrator``."""

    def _run_verification_phase(
        self,
        *,
        spec: StrategySpec,
        trades: List[TradeRecord],
        metrics: BacktestResult,
        market_data: Optional[Dict[str, List[OHLCVBar]]],
        config: BacktestConfig,
        execution_succeeded: bool,
        trades_aligned: bool,
        alignment_reports: List[TradeAlignmentReport],
        all_gate_results: List[QualityGateResult],
        emit: PhaseCallback,
        open_position_entry_reasons: Optional[List[str]] = None,
        runtime_lookahead_violation: bool = False,
    ) -> _VerificationOutcome:
        """Run walk-forward + acceptance, conformance, is_winning resolution.

        Pre: synthesis + alignment loops have settled (``spec`` / ``trades``
        / ``metrics`` are the known-good state). ``execution_succeeded``
        tracks whether the last execution cleared the anomaly gates.
        Post: returns a ``_VerificationOutcome`` carrying possibly-mutated
        ``metrics`` (acceptance_reason / oos_* fields) plus the resolved
        ``is_winning`` flag and the gate-level facts the caller persists.
        Mutates ``all_gate_results`` in place (acceptance + conformance +
        optional fallback gates appended).

        The WINNING/LOSING label is resolved deterministically: a valid run
        (it executed and produced a trade ledger) is winning iff its
        annualized return meets or beats the S&P-500 benchmark
        (``annualized_return_pct >= WINNING_THRESHOLD``). The three branches
        below only run the robustness machinery and record its findings on
        ``acceptance_reason`` / ``all_gate_results`` as narrative caveats —
        they no longer decide the label:
          * Walk-forward succeeded → acceptance_gate findings recorded
          * Walk-forward raised → anomaly recheck with ``dsr_aware=False``
          * Walk-forward disabled / no trades → publication_disabled reason
        """
        # Step 1 — walk-forward evaluation + acceptance gate (when eligible).
        metrics, acceptance_results, walk_forward_failed = self._run_walk_forward_acceptance(
            spec=spec,
            market_data=market_data,
            config=config,
            trades=trades,
            metrics=metrics,
            execution_succeeded=execution_succeeded,
            all_gate_results=all_gate_results,
            emit=emit,
        )

        # Step 2 — deterministic exit-rule conformance against the FINAL
        # (post-alignment) trade ledger. A critical failure vetoes the
        # ``acceptance_reason`` below (never the ``is_winning`` label).
        exit_rule_conformance_passed = self._run_exit_rule_conformance_gate(
            spec=spec,
            trades=trades,
            metrics=metrics,
            config=config,
            execution_succeeded=execution_succeeded,
            all_gate_results=all_gate_results,
            market_data=market_data,
        )

        # Step 3 — realism cycle: verification-phase checks that the trade
        # ledger resembles a real-world trading outcome (symbol breadth +
        # cost-stress today; liquidity / regime / clustering / rule-firing
        # are additive). The cost-stress gate self-skips on legacy
        # single-window or walk-forward-fallback paths where
        # ``config.cost_stress`` is False; enforcement of mandatory
        # cost-stress on winning-candidate runs lives at the production
        # entrypoint, which force-enables the flag. Critical findings feed
        # the veto block below.
        # ``None`` (no reports at all) is RuleFiringRateGate's "nothing to
        # evaluate, self-skip" signal on the custom-code path; an empty
        # ``alignment_findings`` list on a real report is instead read as
        # "every rule is dead" (see that gate's docstring). Those two only
        # stay distinguishable because this method's own early-out above
        # (``if not execution_succeeded or not trades: return []``) and
        # ``DeterministicAlignmentChecker`` both key off the same "no
        # trades" condition — an aligned report for a non-empty ledger is
        # never expected to carry an empty ``alignment_findings``. If that
        # invariant ever breaks, the gate would treat the empty list as a
        # false "all rules dead" verdict rather than skipping.
        realism_results = self._run_realism_gates(
            spec=spec,
            trades=trades,
            metrics=metrics,
            config=config,
            market_data=market_data,
            execution_succeeded=execution_succeeded,
            open_position_entry_reasons=open_position_entry_reasons,
            alignment_findings=alignment_reports[-1].alignment_findings
            if alignment_reports
            else None,
        )
        all_gate_results.extend(realism_results)
        realism_critical = [r for r in realism_results if not r.passed and r.severity == "critical"]
        realism_passed = not realism_critical

        # Step 4 — robustness/audit bookkeeping across the three
        # publication-decision paths; stamps ``acceptance_reason`` and
        # reports whether the upstream gate admitted the run.
        metrics, upstream_admitted = self._resolve_publication_decision(
            metrics=metrics,
            trades=trades,
            market_data=market_data,
            config=config,
            execution_succeeded=execution_succeeded,
            acceptance_results=acceptance_results,
            walk_forward_failed=walk_forward_failed,
            all_gate_results=all_gate_results,
        )

        # Step 5 — deterministic verdict. A *valid* run (it executed and
        # produced a trade ledger) is WINNING iff its annualized return meets
        # or beats the S&P-500 benchmark. The robustness gate outcomes above
        # are recorded on ``acceptance_reason`` / ``all_gate_results`` and
        # surface as narrative caveats, but never change this label. The
        # ``execution_succeeded and trades`` guard is a validity precondition
        # (not a robustness judgement): a run that never produced a real
        # ledger has no genuine return and cannot win.
        is_winning = bool(
            execution_succeeded and trades and metrics.annualized_return_pct >= WINNING_THRESHOLD
        )

        # Step 6 — publication vetoes: surface each robustness failure's cause
        # on ``acceptance_reason`` (caveats only; never change ``is_winning``).
        metrics, upstream_admitted = self._apply_publication_vetoes(
            metrics=metrics,
            execution_succeeded=execution_succeeded,
            trades=trades,
            exit_rule_conformance_passed=exit_rule_conformance_passed,
            all_gate_results=all_gate_results,
            realism_passed=realism_passed,
            realism_critical=realism_critical,
            alignment_reports=alignment_reports,
            trades_aligned=trades_aligned,
            runtime_lookahead_violation=runtime_lookahead_violation,
            upstream_admitted=upstream_admitted,
        )

        # Step 7 — publishability: paper-trading decision distinct from the
        # return-threshold ``is_winning`` label. Existing gate booleans only.
        skip_reason = publishability_skip_reason(
            exit_rule_conformance_passed=exit_rule_conformance_passed,
            realism_passed=realism_passed,
            trades_aligned=trades_aligned,
            runtime_lookahead_violation=runtime_lookahead_violation,
        )
        is_publishable = bool(is_winning and skip_reason is None)

        return _VerificationOutcome(
            metrics=metrics,
            is_winning=is_winning,
            is_publishable=is_publishable,
            upstream_admitted=upstream_admitted,
            acceptance_results=acceptance_results,
            walk_forward_failed=walk_forward_failed,
            exit_rule_conformance_passed=exit_rule_conformance_passed,
            publishability_skip_reason=skip_reason if is_winning and not is_publishable else None,
        )

    def _run_walk_forward_acceptance(
        self,
        *,
        spec: StrategySpec,
        market_data: Optional[Dict[str, List[OHLCVBar]]],
        config: BacktestConfig,
        trades: List[TradeRecord],
        metrics: BacktestResult,
        execution_succeeded: bool,
        all_gate_results: List[QualityGateResult],
        emit: PhaseCallback,
    ) -> Tuple[BacktestResult, List[QualityGateResult], bool]:
        """Run walk-forward evaluation + acceptance gate when eligible.

        Pre: ``metrics`` is the settled single-window backtest result and
        ``all_gate_results`` is the running gate list. Eligibility requires a
        successful execution with trades, fetched ``market_data``, and
        ``config.walk_forward_enabled``.
        Post: returns ``(metrics, acceptance_results, walk_forward_failed)``.
        When eligible and successful, ``metrics`` carries the walk-forward
        fields plus ``acceptance_reason`` / ``n_trials_when_accepted`` and the
        ``acceptance_results`` are appended to ``all_gate_results`` in place.
        When the evaluation raises, ``metrics`` is returned at its last
        successful assignment, ``acceptance_results`` is empty, and
        ``walk_forward_failed=True`` so the caller routes to the fallback
        anomaly recheck. When ineligible, the inputs are returned unchanged
        with an empty result list and ``False``.
        Invariant: never raises — a walk-forward exception is caught and
        converted to the fallback signal.
        """
        if not (
            execution_succeeded
            and trades
            and market_data is not None
            and config.walk_forward_enabled
        ):
            return metrics, [], False
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
            return metrics, acceptance_results, False
        except Exception:
            logger.exception(
                "Walk-forward evaluation failed for %s; falling back to "
                "legacy single-window acceptance",
                spec.strategy_id,
            )
            return metrics, [], True

    def _run_exit_rule_conformance_gate(
        self,
        *,
        spec: StrategySpec,
        trades: List[TradeRecord],
        metrics: BacktestResult,
        config: BacktestConfig,
        execution_succeeded: bool,
        all_gate_results: List[QualityGateResult],
        market_data: Optional[Dict[str, List[OHLCVBar]]] = None,
    ) -> bool:
        """Deterministically verify the engine enforced ``spec.exit_rules``.

        Pre: ``trades`` is the FINAL post-alignment ledger and
        ``all_gate_results`` is the running gate list. ``market_data`` (when
        supplied) carries the run's cached bars so the opt-in trailing-stop
        replay (gated by ``config.exit_rule_trailing_replay_enabled``) can
        reconstruct per-bar watermarks.
        Post: returns ``True`` when no critical conformance finding fired — or
        when the check is skipped because the run did not execute / produced
        no trades — and ``False`` when a critical engine-enforcement leak was
        detected. Appends the conformance results to ``all_gate_results`` in
        place when the check runs.
        """
        if not (execution_succeeded and trades):
            return True
        conformance_gate = ExitRuleConformanceGate()
        conformance_results = conformance_gate.check(
            exit_rules=spec.exit_rules,
            trades=trades,
            diagnostics=metrics.execution_diagnostics,
            config=config,
            timeframe=spec.timeframe,
            market_data=market_data,
        )
        all_gate_results.extend(conformance_results)
        return not any((not r.passed) and r.severity == "critical" for r in conformance_results)

    def _resolve_publication_decision(
        self,
        *,
        metrics: BacktestResult,
        trades: List[TradeRecord],
        market_data: Optional[Dict[str, List[OHLCVBar]]],
        config: BacktestConfig,
        execution_succeeded: bool,
        acceptance_results: List[QualityGateResult],
        walk_forward_failed: bool,
        all_gate_results: List[QualityGateResult],
    ) -> Tuple[BacktestResult, bool]:
        """Record robustness bookkeeping across the three publication paths.

        Pre: exactly one path applies — ``acceptance_results`` is non-empty
        (walk-forward succeeded), ``walk_forward_failed and execution_succeeded``
        (fallback anomaly recheck), or neither (publication disabled).
        Post: returns ``(metrics, upstream_admitted)`` where ``metrics`` may
        carry a stamped ``acceptance_reason`` and ``upstream_admitted`` records
        whether the upstream gate (walk-forward or fallback) admitted the run.
        Mutates ``all_gate_results`` in place only on the fallback path with a
        critical finding (``fallback_``-prefixed gate rows). The WINNING/LOSING
        label is resolved deterministically by the caller after this returns —
        these gates only record caveats.
        """
        upstream_admitted = False
        if acceptance_results:
            upstream_admitted = all(r.passed for r in acceptance_results)
        elif walk_forward_failed and execution_succeeded:
            # Walk-forward fallback: anomaly recheck occurs after refinement
            # in the verification phase. Anomaly checks during refinement
            # ran with ``dsr_aware=True``, which downgraded ``Sharpe > 5.0``
            # from critical to warning on the assumption that OOS DSR would
            # adjudicate. Re-run with ``dsr_aware=False`` and reject if any
            # critical fires — otherwise an obvious overfit could be marked
            # winning on annualized return alone.
            fallback_anomalies = self.anomaly_detector.check(
                metrics,
                trades,
                dsr_aware=False,
                coverage_report=metrics.coverage_report,
                phase="verification",
                market_data=market_data,
            )
            fallback_criticals = [
                g for g in fallback_anomalies if not g.passed and g.severity == "critical"
            ]
            return_ok = metrics.annualized_return_pct >= WINNING_THRESHOLD
            upstream_admitted = return_ok and not fallback_criticals
            if fallback_criticals:
                # Surface the upgraded severities so the persisted gate-
                # result history reflects the true rejection reason.
                self.record_gates(
                    fallback_anomalies, all_gate_results, gate_name_prefix="fallback_"
                )
            # Mirror the fallback gate's own verdict onto
            # ``acceptance_reason`` so consumers don't have to grep
            # ``quality_gate_results`` for ``fallback_`` prefixes.
            if upstream_admitted:
                metrics = metrics.model_copy(
                    update={
                        "acceptance_reason": "walk_forward_fallback_passed: anomaly recheck clean"
                    }
                )
            else:
                fallback_reasons: List[str] = []
                if fallback_criticals:
                    fallback_reasons.append(join_gate_details(fallback_criticals))
                if not return_ok:
                    fallback_reasons.append(
                        f"annualized_return {metrics.annualized_return_pct:.2f}% < "
                        f"{WINNING_THRESHOLD:g}% threshold"
                    )
                metrics = metrics.model_copy(
                    update={
                        "acceptance_reason": (
                            "walk_forward_fallback_rejected: " + "; ".join(fallback_reasons)
                        )
                    }
                )
        else:
            # Self-document why publication was blocked on each else-branch
            # entry path so the persisted record carries a reason rather than
            # an empty ``acceptance_reason``. The label itself is resolved by
            # the deterministic rule in the caller (these runs produced no
            # qualifying return). Ordered most-proximate-cause first: a failed
            # execution is the root cause and subsumes the (necessarily empty)
            # ledger, so it is recorded ahead of the no-trades case. A
            # downstream veto (conformance / realism / alignment / look-ahead)
            # may append its specific cause to whichever reason is stamped here.
            if not execution_succeeded:
                metrics = metrics.model_copy(
                    update={"acceptance_reason": "publication_disabled: execution_failed"}
                )
            elif not trades:
                metrics = metrics.model_copy(
                    update={"acceptance_reason": "publication_disabled: no trades produced"}
                )
            elif not config.walk_forward_enabled:
                metrics = metrics.model_copy(
                    update={"acceptance_reason": "publication_disabled: walk_forward_enabled=False"}
                )
        return metrics, upstream_admitted

    def _apply_publication_vetoes(
        self,
        *,
        metrics: BacktestResult,
        execution_succeeded: bool,
        trades: List[TradeRecord],
        exit_rule_conformance_passed: bool,
        all_gate_results: List[QualityGateResult],
        realism_passed: bool,
        realism_critical: List[QualityGateResult],
        alignment_reports: List[TradeAlignmentReport],
        trades_aligned: bool,
        runtime_lookahead_violation: bool,
        upstream_admitted: bool,
    ) -> Tuple[BacktestResult, bool]:
        """Stamp each unresolved robustness failure onto ``acceptance_reason``.

        Pre: the deterministic ``is_winning`` label has already been resolved
        by the caller; ``exit_rule_conformance_passed`` / ``realism_passed`` /
        ``trades_aligned`` / ``runtime_lookahead_violation`` carry the gate
        verdicts and ``all_gate_results`` is the running gate list.
        Post: returns ``(metrics, upstream_admitted)`` with each applicable
        veto's cause appended to ``acceptance_reason`` via
        ``_apply_veto_to_acceptance_reason`` (replace a stale success reason,
        append a real rejection). These stamps are caveats only and never
        change ``is_winning``.
        Invariant: applies the four vetoes in the same fixed order —
        conformance, realism, alignment, runtime look-ahead.
        """
        if execution_succeeded and trades and not exit_rule_conformance_passed:
            conformance_criticals = [
                r
                for r in all_gate_results
                if r.gate_name == "exit_rule_conformance"
                and not r.passed
                and r.severity == "critical"
            ]
            detail = join_gate_details(conformance_criticals)
            suffix = (
                f"exit_rule_conformance_failed: {detail}"
                if detail
                else "exit_rule_conformance_failed: engine enforcement leaked"
            )
            metrics, upstream_admitted = _apply_veto_to_acceptance_reason(
                metrics, suffix, upstream_admitted=upstream_admitted
            )

        if execution_succeeded and trades and not realism_passed:
            detail = join_gate_details(realism_critical)
            suffix = (
                f"realism_failed: {detail}"
                if detail
                else "realism_failed: realism gates produced a critical finding"
            )
            metrics, upstream_admitted = _apply_veto_to_acceptance_reason(
                metrics, suffix, upstream_admitted=upstream_admitted
            )

        if execution_succeeded and trades and alignment_reports and not trades_aligned:
            last_report = alignment_reports[-1]
            alignment_criticals = [
                f
                for f in last_report.alignment_findings
                if not f.passed and f.severity == "critical"
            ]
            if alignment_criticals:
                detail = "; ".join(
                    f"[{f.check_name}] trade#{f.trade_num}: {f.details}"
                    for f in alignment_criticals[:10]
                )
                if len(alignment_criticals) > 10:
                    detail += f" (+{len(alignment_criticals) - 10} more)"
            else:
                detail = (last_report.rationale or "").strip()
            suffix = (
                f"alignment_unresolved: {detail}"
                if detail
                else "alignment_failed: trades did not implement strategy spec"
            )
            metrics, upstream_admitted = _apply_veto_to_acceptance_reason(
                metrics, suffix, upstream_admitted=upstream_admitted
            )

        # Runtime look-ahead — record the cause as a caveat. The harness traps
        # ``AttributeError`` on forward-field access and surfaces it as
        # ``TradingServiceResult.lookahead_violation=True`` → propagated
        # through ``StrategyRunResult.error_type="lookahead_violation"`` →
        # ``_SynthesisLoopOutcome.runtime_lookahead_violation``. By the
        # time the synthesis loop hands control to verification, an
        # unresolved lookahead means refinement exhausted its budget
        # without producing a clean run; ``execution_succeeded`` is already
        # False, so the deterministic verdict already resolved
        # ``is_winning=False`` via the validity precondition (an invalid run
        # has no qualifying return). This stamp makes the cause explicit in
        # the audit trail instead of the generic ``publication_disabled``
        # reason — it is a caveat only and does not change the label. The
        # sentinel field ``subprocess_attribute_error`` records that the trip
        # point was the harness's AttributeError interceptor (the violating
        # attribute name is not preserved on TradingServiceResult).
        if runtime_lookahead_violation:
            metrics, upstream_admitted = _apply_veto_to_acceptance_reason(
                metrics,
                "lookahead_violation_at_runtime: subprocess_attribute_error",
                upstream_admitted=upstream_admitted,
            )

        return metrics, upstream_admitted
