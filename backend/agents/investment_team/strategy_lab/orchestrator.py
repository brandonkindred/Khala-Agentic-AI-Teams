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

import json
import logging
import math
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Literal, Optional, Sequence, Tuple

from ..execution.benchmarks import benchmark_for_strategy, build_60_40_equity
from ..execution.metrics import (
    bootstrap_sharpe_ci,
    build_equity_curve_from_trades,
    compute_deflated_sharpe,
    summarize_return_moments,
)
from ..execution.regimes import regime_comparison, vix_quartile_subwindows
from ..execution.risk_filter import _RISK_LIMIT_TIGHTEN_DIRECTION, RiskLimits
from ..execution.walk_forward import (
    build_purged_walk_forward,
    filter_trades_in_fold_training,
    filter_trades_in_range,
    max_hold_days_from_trades,
)
from ..market_data_service import MarketDataService, OHLCVBar
from ..models import (
    BacktestConfig,
    BacktestExecutionDiagnostics,
    BacktestRecord,
    BacktestResult,
    StrategyLabRecord,
    StrategySpec,
    TradeRecord,
    get_fee_defaults,
)
from ..signal_intelligence_models import SignalIntelligenceBriefV1
from ..trade_simulator import compute_metrics
from ..trading_service.modes.sandbox_compat import StrategyRunResult, run_strategy_code
from .agents.alignment import AlignmentAuditError, TradeAlignmentAgent, TradeAlignmentReport
from .agents.analysis import AnalysisAgent
from .agents.ideation import IdeationAgent
from .agents.refinement import RefinementAgent
from .agents.zero_trade_repair import ZeroTradeRepairAgent
from .coverage_probe import format_coverage_report, run_coverage_stage, should_run_probes
from .exceptions import SpecImplementabilityError
from .quality_gates.acceptance_gate import AcceptanceGate, summarize_acceptance_reason
from .quality_gates.backtest_anomaly import BacktestAnomalyDetector
from .quality_gates.code_safety import CodeSafetyChecker
from .quality_gates.convergence_tracker import ConvergenceTracker
from .quality_gates.exit_rule_conformance import ExitRuleConformanceGate
from .quality_gates.models import QualityGateResult, StrategyLabPhase
from .quality_gates.spec_readiness import SpecReadinessGate
from .quality_gates.strategy_validator import StrategySpecValidator
from .quality_gates.target_symbol_coverage import TargetSymbolCoverageGate
from .spec_dsl import DEFAULT_SIZING_PAYLOAD
from .zero_trade_repair import ZeroTradeRepairer

logger = logging.getLogger(__name__)

PhaseCallback = Callable[[str, Dict[str, Any]], None]


@dataclass(frozen=True)
class _MarketDataFetch:
    """Issue #525 — return envelope for ``_fetch_market_data``.

    Carries the OHLCV payload alongside the audit trail of the symbols the
    fetch was asked to retrieve and the symbols that actually returned
    usable bars. Both lists feed ``BacktestRecord`` so reviewers can see
    when a fetch silently dropped tickers without re-running the cycle.
    """

    data: Optional[Dict[str, List[OHLCVBar]]]
    requested_symbols: List[str]
    fetched_symbols: List[str]


@dataclass
class _AlignmentLoopOutcome:
    """Bundle of state mutated by ``_run_trade_alignment_loop``.

    The trade-alignment loop can replace the run's known-good
    ``spec`` / ``code`` / ``trades`` / ``metrics`` if it commits a fix,
    and tracks attempt strings + per-round reports the caller consumes.
    Returning a single dataclass keeps ``_run_design_attempt``'s
    unpacking explicit and small.
    """

    spec: StrategySpec
    code: str
    trades: List[TradeRecord]
    metrics: "BacktestResult"
    alignment_attempts: List[str]
    alignment_reports: List["TradeAlignmentReport"]
    trades_aligned: bool

    @property
    def alignment_rounds(self) -> int:
        return len(self.alignment_attempts)


# Refinement output is code-only post-#543. Anything else the LLM emits is
# logged + discarded by ``_apply_updates``; ``risk_limits`` is the lone
# exception, handled with tighten-only semantics.
#
# NOTE: ``RefinementAgent`` enforces the same contract on its side via
# ``_ALLOWED_OUTPUT_KEYS`` / ``_PASSTHROUGH_FOR_ORCHESTRATOR`` in
# ``agents/refinement.py``. The duplication is intentional — agent-side
# narrowing is a first line of defense; orchestrator-side narrowing is
# authoritative. Keep the two passthrough sets in sync.
_REFINEMENT_ALLOWED_KEYS = frozenset({"changes_made"})
_REFINEMENT_PASSTHROUGH_KEYS = frozenset({"risk_limits"})

# Threshold (per ``failure_phase``) at which repeated spec-mutation attempts
# from the refinement agent trip ``SpecImplementabilityError`` and route the
# cycle back to ideation.
_SPEC_MUTATION_TRIP_THRESHOLD = 3

# Outer-loop cap on how many times ``run_cycle`` re-enters ideation after a
# ``SpecImplementabilityError``. ``MAX_DESIGN_REENTRIES = 2`` permits the
# original ideation + 2 re-attempts before short-circuiting.
MAX_DESIGN_REENTRIES = 2

MAX_CODE_REFINEMENT_ROUNDS = 50
# Maximum number of trade-alignment problem-solving rounds. Each round
# audits the executed trades against the spec and, if misaligned, asks the
# alignment agent to rewrite the Python code; the new code is sent back
# through the sandbox for a fresh backtest. The cap prevents runaway loops
# when the agent cannot converge.
MAX_ALIGNMENT_ROUNDS = 10
# Single-window annualized-return floor consulted only when the walk-forward
# acceptance gate is unavailable (i.e. ``_evaluate_walk_forward`` raised and
# we drop into the fallback path). Issue #247 replaced this scalar with the
# composite ``AcceptanceGate`` (OOS DSR + IS→OOS degradation + OOS trade
# count + regime beats) on the primary publication path, and #529 removed
# the legacy ``walk_forward_enabled=False`` branch that previously used this
# threshold as a publication gate.
WINNING_THRESHOLD = 8.0

# Cap on `last_order_events` included in the refinement-prompt diagnostics
# block. The model already trims to 20; 10 is enough signal for the LLM to
# spot the failure pattern while keeping the JSON line under ~1 KB.
_DIAGNOSTICS_LAST_EVENTS_CAP = 10


def _maybe_attach_coverage_report(
    *,
    metrics: BacktestResult,
    spec: StrategySpec,
    market_data: Dict[str, List[OHLCVBar]],
    config: BacktestConfig,
    exec_result: StrategyRunResult,
) -> None:
    """Run the #451 coverage stage and stamp the report onto ``metrics``.

    The ``spec`` argument MUST carry the same ``strategy_code`` that was
    handed to ``run_strategy_code`` to produce ``exec_result``. The
    alignment and zero-trade-repair paths use a ``proposed_spec`` variant
    of the surrounding spec; pass that, not the loop-level ``spec``,
    otherwise the static probe will analyse stale source.

    No-ops when ``should_run_probes`` says the run isn't zero/low-trade —
    successful runs keep ``metrics.coverage_report = None`` and pay no
    probe cost.
    """
    if should_run_probes(exec_result.execution_diagnostics):
        metrics.coverage_report = run_coverage_stage(
            spec=spec,
            market_data=market_data,
            config=config,
            exec_result=exec_result,
            run_strategy_code_fn=run_strategy_code,
        )


def _format_execution_diagnostics(
    diagnostics: Optional[BacktestExecutionDiagnostics],
) -> str:
    """Render a compact JSON block of execution diagnostics for the
    refinement prompt (issue #414, part of #404).

    Returns an empty string when diagnostics is missing or the executor
    couldn't classify a zero-trade failure — healthy backtests must not
    bloat the prompt. When a ``zero_trade_category`` is present, returns a
    single line ``"Execution Diagnostics: {<json>}"`` whose JSON payload is
    stable-key-sorted and compact. ``last_order_events`` is capped to the
    most recent ``_DIAGNOSTICS_LAST_EVENTS_CAP`` entries.
    """
    if diagnostics is None or diagnostics.zero_trade_category is None:
        return ""

    payload = diagnostics.model_dump(mode="json", exclude_none=True)
    events = payload.get("last_order_events") or []
    if len(events) > _DIAGNOSTICS_LAST_EVENTS_CAP:
        payload["last_order_events"] = events[-_DIAGNOSTICS_LAST_EVENTS_CAP:]

    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return f"Execution Diagnostics: {encoded}"



def _apply_veto_to_acceptance_reason(
    metrics: BacktestResult,
    suffix: str,
    *,
    upstream_admitted: bool,
) -> tuple[BacktestResult, bool]:
    """Stamp a publication veto's cause onto ``metrics.acceptance_reason``.

    Both the conformance veto (#527) and the alignment veto (#529)
    follow the same shape: replace a stale success-style upstream
    reason; append to a real upstream rejection. Returns the updated
    ``metrics`` and ``False`` for the new ``upstream_admitted``, so a
    subsequent veto on the same run appends to this one rather than
    overwriting it.

    The delimiter is ``" | "`` (not ``"; "`` which
    :func:`summarize_acceptance_reason` uses between failing gates)
    so downstream parsers can disambiguate the veto boundary from
    gate-internal boundaries.

    Whitespace-only upstream reasons (``None``, ``""``, ``"   "``)
    collapse to the suffix alone — never produces
    ``"   | <suffix>"`` with an empty left side.
    """
    prior = (metrics.acceptance_reason or "").strip()
    if prior and not upstream_admitted:
        combined = f"{prior} | {suffix}"
    else:
        combined = suffix
    return metrics.model_copy(update={"acceptance_reason": combined}), False


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
        self.zero_trade_repair_agent = ZeroTradeRepairAgent()
        self.analysis_agent = AnalysisAgent()
        self.strategy_validator = StrategySpecValidator()
        self.code_safety_checker = CodeSafetyChecker()
        self.anomaly_detector = BacktestAnomalyDetector()
        self.acceptance_gate = AcceptanceGate()
        self.target_symbol_coverage_gate = TargetSymbolCoverageGate()
        self.convergence_tracker = convergence_tracker or ConvergenceTracker()
        self.market_data_service = MarketDataService()
        # SpecReadinessGate is wired to the live MarketDataService so Rule 5
        # sizes against real recent closes rather than a synthetic default.
        self.spec_readiness_gate = SpecReadinessGate(
            market_sample_provider=self._readiness_price_provider,
        )
        # Zero-trade repair sub-pipeline. Lives in its own module so the
        # ~340 lines of branching logic + the four gates it threads through
        # don't bloat this class. The repairer reads gate instances off
        # ``self`` so no API surface is duplicated.
        self.zero_trade_repairer = ZeroTradeRepairer(self)
        # Per-failure_phase counter of consecutive refinement rounds where
        # the LLM emitted stray spec-mutating keys. Reset at the start of
        # each ``_run_design_attempt`` and tripped when any phase hits
        # ``_SPEC_MUTATION_TRIP_THRESHOLD``.
        self._consecutive_spec_mutation_rounds: Dict[str, int] = {}

    def _readiness_price_provider(self, symbol: str, asset_class: str) -> float:
        """Recent-close lookup wired into ``SpecReadinessGate.Rule 5``.

        Pre: ``symbol`` and ``asset_class`` are non-empty strings.
        Post: returns either a strictly positive finite price *or*
        ``float("nan")`` when no live price is available — so Rule 5's
        non-finite check fails the gate closed instead of silently sizing
        against a synthetic placeholder that could let a high-priced spec
        slip through.

        The cycle never crashes on a data-fetch failure: the exception is
        logged and the NaN return turns into a critical readiness failure
        the operator can act on, rather than a "looks fine, then runs to
        zero trades" silent error downstream.
        """
        assert isinstance(symbol, str) and symbol, "symbol must be a non-empty str"
        assert isinstance(asset_class, str) and asset_class, "asset_class must be a non-empty str"
        try:
            bars = self.market_data_service.fetch_ohlcv(symbol, asset_class, days=5)
            if bars:
                close = float(bars[-1].close)
                if close > 0:
                    return close
        except Exception as exc:  # noqa: BLE001 — fail-closed via NaN below
            logger.debug(
                "readiness price provider returned NaN for %s/%s: %s",
                symbol,
                asset_class,
                exc,
            )
        return float("nan")

    def record_gates(
        self,
        results: List[QualityGateResult],
        all_gate_results: Optional[List[QualityGateResult]] = None,
        *,
        refinement_round: Optional[int] = None,
        gate_name_prefix: str = "",
    ) -> List[QualityGateResult]:
        """Stamp metadata on each result; optionally extend a running list.

        Pre: ``results`` is a list of ``QualityGateResult``;
        ``all_gate_results`` is either ``None`` (stamp only) or another list
        to extend with ``results``; ``refinement_round`` is an int (``-1`` for
        pre-synthesis sites) or ``None`` to leave the existing value.
        Post: every entry in ``results`` carries the supplied
        ``refinement_round`` (when not ``None``); if ``gate_name_prefix`` is
        non-empty the prefix is applied to ``gate_name``. When
        ``all_gate_results`` is provided it is extended in place. ``results``
        is returned to allow chaining.
        """
        assert isinstance(results, list) and all(
            isinstance(g, QualityGateResult) for g in results
        ), "results must be a list of QualityGateResult"
        for g in results:
            if refinement_round is not None:
                g.refinement_round = refinement_round
            if gate_name_prefix:
                g.gate_name = f"{gate_name_prefix}{g.gate_name}"
        if all_gate_results is not None:
            all_gate_results.extend(results)
        return results

    def build_orchestrator_gate(
        self,
        name: str,
        *,
        phase: StrategyLabPhase,
        severity: Literal["info", "warning", "critical"] = "critical",
        details: str,
        refinement_round: int = 0,
    ) -> QualityGateResult:
        """Bespoke result for failures the orchestrator detects directly.

        Pre: ``name`` is a non-empty string; ``phase`` is one of the four
        valid labels; ``details`` is a non-empty string.
        Post: returns a ``QualityGateResult`` whose ``passed`` flag is
        derived from ``severity`` (info → True, warning/critical → False).
        """
        assert name, "gate name must be non-empty"
        assert details, "details must be non-empty"
        return QualityGateResult(
            gate_name=name,
            phase=phase,
            passed=severity == "info",
            severity=severity,
            details=details,
            refinement_round=refinement_round,
        )

    def run_cycle(
        self,
        prior_records: List[StrategyLabRecord],
        config: BacktestConfig,
        signal_brief: Optional[SignalIntelligenceBriefV1] = None,
        on_phase: Optional[PhaseCallback] = None,
        exclude_asset_classes: Optional[List[str]] = None,
    ) -> StrategyLabRecord:
        """Run one full strategy lab cycle: ideate → code → backtest → analyze.

        Wraps ``_run_design_attempt`` in an outer retry loop bounded by
        ``MAX_DESIGN_REENTRIES`` (#543): when refinement detects the spec
        is unimplementable (`SpecImplementabilityError`), the cycle
        re-enters ideation with the failure evidence appended as a
        convergence directive. On exhaustion, persists a short-circuit
        record with ``status='failed: spec_unimplementable'``.

        Returns a StrategyLabRecord with the final result.
        """
        emit = on_phase or (lambda phase, data: None)

        # Gather convergence directives once — appended to on loopback.
        directives: List[str] = []
        stall_dir = self.convergence_tracker.get_stall_directive()
        if stall_dir:
            directives.append(stall_dir)
        diversity_dir = self.convergence_tracker.get_diversity_directive()
        if diversity_dir:
            directives.append(diversity_dir)
        directives.extend(self.convergence_tracker.get_failure_directives())

        last_evidence: Optional[str] = None
        last_spec: Optional[StrategySpec] = None
        last_code: str = ""
        last_failure_phase: Optional[str] = None
        for design_attempt in range(MAX_DESIGN_REENTRIES + 1):
            try:
                return self._run_design_attempt(
                    prior_records=prior_records,
                    config=config,
                    signal_brief=signal_brief,
                    emit=emit,
                    exclude_asset_classes=exclude_asset_classes,
                    directives=directives,
                )
            except SpecImplementabilityError as exc:
                last_evidence = exc.evidence
                last_spec = exc.last_spec
                last_code = exc.last_code
                last_failure_phase = exc.failure_phase
                if design_attempt >= MAX_DESIGN_REENTRIES:
                    break
                emit(
                    "ideating",
                    {
                        "sub_phase": "loopback",
                        "design_attempt": design_attempt + 1,
                        "evidence": exc.evidence,
                        "failure_phase": exc.failure_phase,
                    },
                )
                directives.append(f"PREVIOUS SPEC UNIMPLEMENTABLE: {exc.evidence}")

        # Re-entry budget exhausted. The exception's ``last_spec`` /
        # ``last_code`` carry the just-pre-mutation state from the most
        # recent ``_apply_updates`` raise. They are required on the
        # exception type, but guard defensively in case a future raiser
        # somehow violates the contract — surface a clear runtime error
        # rather than crashing in ``_build_short_circuit_record`` with a
        # misleading traceback.
        if last_spec is None or last_evidence is None:
            raise RuntimeError(
                "SpecImplementabilityError raised without last_spec/evidence; "
                "cannot build short-circuit record. This is a bug in a refinement "
                "code path; please file an issue with the run logs."
            )
        return self._build_short_circuit_record(
            spec=last_spec,
            config=config,
            code=last_code,
            original_spec=last_spec,
            original_code=last_code,
            rationale="",
            all_gate_results=[],
            refinement_attempts=[],
            short_circuit_status="failed: spec_unimplementable",
            short_circuit_reason=(
                f"Spec unimplementable after {MAX_DESIGN_REENTRIES + 1} design attempts "
                f"(last failure_phase={last_failure_phase}): {last_evidence}"
            ),
            emit=emit,
        )

    def _run_pre_synthesis_phase(
        self,
        *,
        spec: StrategySpec,
        config: BacktestConfig,
        all_gate_results: List[QualityGateResult],
        code: str,
        original_spec: StrategySpec,
        original_code: str,
        rationale: str,
        refinement_attempts: List[Dict[str, Any]],
        emit: PhaseCallback,
    ) -> Optional[StrategyLabRecord]:
        """Run spec validation + readiness gate before the refinement loop.

        Pre: ``spec`` is a constructed ``StrategySpec``; ``all_gate_results``
        is the orchestrator's running gate list that the caller persists.
        Post: returns a short-circuit ``StrategyLabRecord`` when a critical
        gate fires (and ``all_gate_results`` is extended in place with the
        pre-synthesis gates); returns ``None`` to signal the caller can
        continue into the synthesis refinement loop.

        The "strategy_code is missing" critical from StrategySpecValidator
        is deliberately filtered: post-ideation we always have *some* code
        (the loop's existing safety + regeneration paths repair degenerate
        inputs), so short-circuiting on that critical would regress a
        recoverable case into an outright failure.
        """
        pre_spec_gates_raw = self.strategy_validator.validate(spec)
        pre_spec_gates = [
            g
            for g in pre_spec_gates_raw
            if not (g.severity == "critical" and g.details.startswith("strategy_code is missing"))
        ]
        # SpecReadinessGate (design phase) — critical here flows into the
        # same short-circuit path as StrategySpecValidator critical so the
        # synthesis loop cannot run on an unimplementable spec.
        readiness_design = self.spec_readiness_gate.validate(
            spec, phase="design", backtest_config=config
        )
        pre_spec_gates.extend(readiness_design)
        self.record_gates(pre_spec_gates, all_gate_results, refinement_round=-1)

        criticals = [g for g in pre_spec_gates if not g.passed and g.severity == "critical"]
        if not criticals:
            return None

        emit(
            "coding",
            {
                "sub_phase": "failed",
                "phase": "pre_synthesis",
                "checks_total": len(pre_spec_gates),
                "checks_passed": sum(1 for g in pre_spec_gates if g.passed),
            },
        )
        return self._build_short_circuit_record(
            spec=spec,
            config=config,
            code=code,
            original_spec=original_spec,
            original_code=original_code,
            rationale=rationale,
            all_gate_results=all_gate_results,
            refinement_attempts=refinement_attempts,
            short_circuit_status="failed: spec_validation",
            short_circuit_reason=(
                "Spec validation failed before code synthesis: "
                + "; ".join(g.details for g in criticals)
            ),
            emit=emit,
        )

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
    ) -> _AlignmentLoopOutcome:
        """Run the trade-alignment audit loop after the synthesis loop settles.

        Pre: synthesis loop has produced (``code``, ``spec``, ``trades``,
        ``metrics``) plus ``market_data`` was fetched at least once and
        ``execution_succeeded`` tracks whether the last execution cleared
        the anomaly gates.
        Post: returns an ``_AlignmentLoopOutcome`` carrying the (possibly
        updated) ``spec`` / ``code`` / ``trades`` / ``metrics`` plus the
        attempt-string history and per-round reports the caller persists.
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
            )

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
                self.build_orchestrator_gate(
                    "trade_alignment",
                    phase="verification",
                    severity=gate_severity,  # type: ignore[arg-type]
                    details=gate_details,
                    refinement_round=align_round,
                )
            )

            if report.aligned:
                emit(
                    "aligning",
                    {"sub_phase": "aligned", "alignment_round": align_round},
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

            if not report.proposed_code:
                emit(
                    "aligning",
                    {"sub_phase": "no_proposed_fix", "alignment_round": align_round},
                )
                break

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
                break

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
                    self.build_orchestrator_gate(
                        "alignment_code_execution",
                        phase="verification",
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

            new_trades = align_exec.trades
            new_metrics = compute_metrics(
                new_trades, config.initial_capital, config.start_date, config.end_date
            )

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

            anomaly_gates = self.anomaly_detector.check(
                new_metrics,
                new_trades,
                dsr_aware=config.walk_forward_enabled,
                diagnostics=align_exec.execution_diagnostics,
                coverage_report=new_metrics.coverage_report,
                phase="verification",
            )
            self.record_gates(
                anomaly_gates,
                all_gate_results,
                refinement_round=align_round,
                gate_name_prefix="alignment_",
            )
            critical_anomalies = [
                g for g in anomaly_gates if not g.passed and g.severity == "critical"
            ]
            if critical_anomalies:
                diagnostics_block = _format_execution_diagnostics(
                    align_exec.execution_diagnostics
                )
                emit_payload: Dict[str, Any] = {
                    "sub_phase": "anomaly_detected",
                    "alignment_round": align_round,
                    "details": "; ".join(g.details for g in critical_anomalies)[:400],
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
                break

            # All gates passed — commit the proposal as the new known-good
            # state and continue to the next audit.
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

        return _AlignmentLoopOutcome(
            spec=spec,
            code=code,
            trades=trades,
            metrics=metrics,
            alignment_attempts=alignment_attempts,
            alignment_reports=alignment_reports,
            trades_aligned=bool(alignment_reports and alignment_reports[-1].aligned),
        )

    def _run_design_attempt(
        self,
        *,
        prior_records: List[StrategyLabRecord],
        config: BacktestConfig,
        signal_brief: Optional[SignalIntelligenceBriefV1],
        emit: PhaseCallback,
        exclude_asset_classes: Optional[List[str]],
        directives: List[str],
    ) -> StrategyLabRecord:
        """One design+refinement attempt (#543). May raise
        ``SpecImplementabilityError`` to signal a need to re-enter
        ideation; the outer ``run_cycle`` catches and re-routes."""
        # Reset per-attempt counters so a re-entry starts fresh.
        self._consecutive_spec_mutation_rounds = {}

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
            # Issue #537: ideation must declare a timeframe. Default to "1d"
            # if the LLM forgot the field — the prompt makes it mandatory.
            timeframe=strategy_dict.get("timeframe") or "1d",
            entry_rules=strategy_dict.get("entry_rules", []),
            exit_rules=strategy_dict.get("exit_rules", []),
            sizing=strategy_dict.get("sizing", DEFAULT_SIZING_PAYLOAD),
            target_symbols=strategy_dict.get("target_symbols", []),
            risk_limits=strategy_dict.get("risk_limits", {}),
            speculative=strategy_dict.get("speculative", False),
            strategy_code=code,
        )

        # Snapshot ideation outputs so reviewers can compare against any
        # refinement-driven mutation persisted on the final record (#547).
        original_spec = spec.model_copy(deep=True)
        original_code = code

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
        zero_trade_attempts: List[str] = []
        trades: List[TradeRecord] = []
        metrics = compute_metrics([], config.initial_capital, config.start_date, config.end_date)
        execution_succeeded = False
        market_data: Optional[Dict[str, List[OHLCVBar]]] = None
        # Issue #525 — audit trail of the symbol universe the fetch was
        # asked to retrieve and the symbols that actually returned bars.
        # Persisted on ``BacktestRecord`` so reviewers can see when a
        # fetch silently dropped tickers.
        requested_symbols: List[str] = []
        fetched_symbols: List[str] = []
        # #547 item 7: track whether the refinement loop exhausted the round
        # cap so the persisted record's ``status`` is queryable rather than
        # buried in logs. Set at each of the three break-on-max sites
        # (validation / execution / evaluation phases).
        max_rounds_exhausted = False

        # ── Phase 1b: PRE-SYNTHESIS SPEC GATING (#547 item 1) ─────────
        # Validate the ideation-time spec ONCE before entering the
        # refinement loop. Refinement is code-only post-#547 item 2, so
        # the spec cannot drift between rounds; revalidating per round
        # was redundant. A critical SPEC failure here short-circuits the
        # cycle without ever calling run_strategy_code or fetching
        # market data.
        #
        # The "strategy_code is missing" critical is excluded from
        # short-circuit eligibility AND from the persisted gate history:
        # that's a code-generation failure (ideation produced an empty /
        # whitespace strategy_code), and the refinement loop's existing
        # code-safety + regeneration paths are equipped to repair it.
        # Short-circuiting on that critical would regress a previously-
        # recoverable case into an outright failure; persisting it would
        # leave a permanently-unresolved critical on the record (the
        # generic refinement loop never re-runs StrategySpecValidator),
        # which would also reach convergence_tracker.record() as an
        # unresolved spec failure.
        pre_synthesis = self._run_pre_synthesis_phase(
            spec=spec,
            config=config,
            all_gate_results=all_gate_results,
            code=code,
            original_spec=original_spec,
            original_code=original_code,
            rationale=rationale,
            refinement_attempts=refinement_attempts,
            emit=emit,
        )
        if pre_synthesis is not None:
            return pre_synthesis

        # ── Phase 2: CODE REFINEMENT LOOP ─────────────────────────────
        # Iterate up to MAX_CODE_REFINEMENT_ROUNDS, refining the
        # generated code for correctness, performance, syntax errors,
        # build errors, runtime errors, and backtest anomalies (zero
        # trades, implausible returns, etc.).  The loop exits only when
        # all quality gates pass AND the backtest produces sound results.

        for round_num in range(MAX_CODE_REFINEMENT_ROUNDS):
            round_gate_results: List[QualityGateResult] = []

            # ── 2a: VALIDATE (code safety only — spec was validated
            #       pre-synthesis and is immutable for this cycle, see
            #       #547 items 1 & 2).
            emit("coding", {"sub_phase": "started", "refinement_round": round_num})
            # Re-run SpecReadinessGate on the first synthesis round.
            # Precondition: design-phase readiness passed. Postcondition:
            # a critical failure here means the spec was mutated between
            # design exit and synthesis entry — the same short-circuit
            # path that fires for any other critical synthesis-phase gate
            # picks it up. Skipped on round_num > 0 because the spec is
            # immutable across the synthesis loop.
            if round_num == 0:
                round_gate_results.extend(
                    self.spec_readiness_gate.validate(
                        spec, phase="synthesis", backtest_config=config
                    )
                )
            code_gates = self.code_safety_checker.check(code, spec)
            round_gate_results.extend(code_gates)
            self.record_gates(round_gate_results, all_gate_results, refinement_round=round_num)

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
                    spec = self._apply_updates(spec, updates, code, failure_phase="validation")
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
                    max_rounds_exhausted = True
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
                fetch = self._fetch_market_data(spec, config)
                # Issue #525 — record requested/fetched on every cycle,
                # even when the fetch returns nothing usable, so the
                # audit trail captures intent on failed runs too.
                requested_symbols = list(fetch.requested_symbols)
                fetched_symbols = list(fetch.fetched_symbols)
                market_data = fetch.data
                if not market_data:
                    all_gate_results.append(
                        self.build_orchestrator_gate(
                            "market_data",
                            phase="synthesis",
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

                # Issue #526 — fail closed if the fetched universe doesn't
                # include the spec's target_symbols. Code refinement can't
                # fix this (the data simply isn't there), so we break out
                # the same way the no-market-data branch above does.
                fetch_coverage_gates = self.target_symbol_coverage_gate.check_fetch(
                    spec, requested_symbols, fetched_symbols
                )
                self.record_gates(
                    fetch_coverage_gates, all_gate_results, refinement_round=round_num
                )
                if any(not g.passed and g.severity == "critical" for g in fetch_coverage_gates):
                    break

            # ── 2c: EXECUTE (syntax / runtime correctness) ───────────
            emit("backtesting", {"sub_phase": "running_code", "refinement_round": round_num})
            exec_result = run_strategy_code(code, market_data, config, strategy=spec)

            if not exec_result.success:
                all_gate_results.append(
                    self.build_orchestrator_gate(
                        "code_execution",
                        phase="synthesis",
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
                    spec = self._apply_updates(spec, updates, code, failure_phase="execution")
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
                    max_rounds_exhausted = True
                    break

            # ── 2d: COLLECT TRADES ────────────────────────────────────
            # TradingService has already finalised trades through
            # FillSimulator, so the legacy raw-trade validation step is a
            # no-op here. Kept the same ``trades`` variable name so the
            # rest of the loop is untouched.
            trades = exec_result.trades

            # Issue #526 — fail closed when the ledger contains symbols
            # outside spec.target_symbols. Uses ``max_rounds_exhausted`` to
            # leave ``execution_succeeded=False`` so ``is_winning`` stays
            # False at the acceptance gate (same precedent as the
            # max-refinement-rounds anomaly branch below).
            trade_coverage_gates = self.target_symbol_coverage_gate.check_trades(spec, trades)
            self.record_gates(
                trade_coverage_gates, all_gate_results, refinement_round=round_num
            )
            if any(not g.passed and g.severity == "critical" for g in trade_coverage_gates):
                max_rounds_exhausted = True
                break

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

            # Issue #451 — attach a deterministic CoverageReport when the
            # run is zero/low-trade. Successful runs pay no probe cost.
            _maybe_attach_coverage_report(
                metrics=metrics,
                spec=spec,
                market_data=market_data,
                config=config,
                exec_result=exec_result,
            )

            anomaly_gates = self.anomaly_detector.check(
                metrics,
                trades,
                dsr_aware=config.walk_forward_enabled,
                diagnostics=exec_result.execution_diagnostics,
                coverage_report=metrics.coverage_report,
            )
            self.record_gates(anomaly_gates, all_gate_results, refinement_round=round_num)

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
                    diagnostics_block = _format_execution_diagnostics(
                        exec_result.execution_diagnostics
                    )
                    if diagnostics_block:
                        failure_details = f"{failure_details}\n{diagnostics_block}"
                    coverage_block = format_coverage_report(metrics.coverage_report)
                    if coverage_block:
                        failure_details = f"{failure_details}\n{coverage_block}"

                    # Issue #405 — specialized zero-trade repair branch.
                    # If the critical anomaly carries a deterministic
                    # ``zero_trade_category``, ask the targeted repair
                    # agent first. On a successful repair the proposal
                    # has already passed code-safety, a fresh backtest,
                    # and the anomaly gates, so we commit it and re-
                    # enter the loop. On a failed proposal we fall
                    # through to the generic refinement agent so the
                    # existing loop semantics are preserved.
                    diag = exec_result.execution_diagnostics
                    if (
                        diag is not None
                        and diag.zero_trade_category is not None
                        and market_data is not None
                    ):
                        zt_outcome = self.zero_trade_repairer.try_repair(
                            spec=spec,
                            code=code,
                            exec_result=exec_result,
                            coverage_report=metrics.coverage_report,
                            market_data=market_data,
                            config=config,
                            zero_trade_attempts=zero_trade_attempts,
                            round_num=round_num,
                            emit=emit,
                        )
                        all_gate_results.extend(zt_outcome.new_gates)
                        if zt_outcome.committed:
                            assert zt_outcome.new_spec is not None
                            assert zt_outcome.new_metrics is not None
                            assert zt_outcome.new_exec_result is not None
                            code = zt_outcome.new_code
                            spec = zt_outcome.new_spec
                            trades = zt_outcome.new_trades
                            metrics = zt_outcome.new_metrics
                            exec_result = zt_outcome.new_exec_result
                            refinement_attempts.append(
                                f"zero-trade repair: {zt_outcome.changes_made}"
                                if zt_outcome.changes_made
                                else "zero-trade repair"
                            )
                            emit(
                                "coding",
                                {
                                    "sub_phase": "refined",
                                    "refinement_round": round_num,
                                    "changes_made": (
                                        zt_outcome.changes_made or "zero-trade repair"
                                    ),
                                    "via": "zero_trade_repair",
                                },
                            )
                            continue

                    updates, code = self._refine(
                        spec,
                        code,
                        "evaluation (backtest anomaly)",
                        failure_details,
                        metrics,
                        refinement_attempts,
                    )
                    spec = self._apply_updates(spec, updates, code, failure_phase="evaluation")
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
                    # Do NOT flip execution_succeeded — even if the code is
                    # technically correct, the cycle exhausted its rounds on
                    # an unresolved anomaly. Leaving execution_succeeded=False
                    # ensures is_winning stays False so paper-trading does
                    # not fire on a "failed: max_refinement_rounds" record
                    # (#547 review feedback).
                    max_rounds_exhausted = True
                    break

            # All gates passed — code is clean and backtest is sound
            execution_succeeded = True
            break

        # ── Phase 2.5: TRADE ALIGNMENT LOOP ───────────────────────────
        alignment_outcome = self._run_trade_alignment_loop(
            spec=spec,
            code=code,
            trades=trades,
            metrics=metrics,
            market_data=market_data,
            config=config,
            execution_succeeded=execution_succeeded,
            all_gate_results=all_gate_results,
            emit=emit,
        )
        spec = alignment_outcome.spec
        code = alignment_outcome.code
        trades = alignment_outcome.trades
        metrics = alignment_outcome.metrics
        alignment_rounds = alignment_outcome.alignment_rounds
        trades_aligned = alignment_outcome.trades_aligned
        # ``alignment_reports`` flows through to the alignment-veto guard
        # below; the guard reads it to know whether the audit actually ran
        # (skipped when ``market_data`` is None).
        alignment_reports = alignment_outcome.alignment_reports

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

        # ── Exit-rule conformance ─────────────────────────────────────
        # Issue #527 — deterministic check that the engine actually
        # enforced ``spec.exit_rules`` against the FINAL trade ledger
        # (post-alignment-loop). Critical failure vetoes ``is_winning``
        # below; results are appended to ``all_gate_results`` for the
        # persisted record.
        exit_rule_conformance_passed = True
        if execution_succeeded and trades:
            conformance_gate = ExitRuleConformanceGate()
            conformance_results = conformance_gate.check(
                exit_rules=spec.exit_rules,
                trades=trades,
                diagnostics=metrics.execution_diagnostics,
                config=config,
                timeframe=spec.timeframe,
            )
            all_gate_results.extend(conformance_results)
            exit_rule_conformance_passed = not any(
                (not r.passed) and r.severity == "critical" for r in conformance_results
            )

        # ── Resolve is_winning ────────────────────────────────────────
        # Publication requires (a) the walk-forward acceptance gate (or
        # its overfit-recheck fallback) AND (b) trade-alignment
        # convergence (``trades_aligned``, #529). A strategy whose final
        # ``TradeAlignmentReport.aligned`` is False — because the loop hit
        # ``MAX_ALIGNMENT_ROUNDS`` or broke early with no proposed fix —
        # cannot be marked winning regardless of metrics, since the
        # executed trades do not implement the strategy spec.
        #
        # Walk-forward fallback: anomaly checks during refinement ran with
        # ``dsr_aware=True``, which downgraded the ``Sharpe > 5.0`` flag
        # from critical to warning on the assumption that the OOS DSR
        # would adjudicate. With AcceptanceGate unavailable, re-run the
        # anomaly checks with ``dsr_aware=False`` and reject if any
        # critical fires — otherwise an obvious overfit could still be
        # marked winning on annualized return alone.
        #
        # The legacy ``walk_forward_enabled=False`` single-window
        # ``WINNING_THRESHOLD`` branch was removed (#529): a bare
        # annualized-return scalar with no DSR correction is not a
        # publication gate. Runs configured without walk-forward still
        # execute and generate a narrative, but cannot be marked winning.
        # ``upstream_admitted`` records whether the upstream publication
        # gate (walk-forward acceptance, or its anomaly-recheck fallback)
        # said "admit". It feeds the alignment-augmentation block below:
        # a success-style ``acceptance_reason`` becomes stale the moment
        # alignment vetoes a run, so we REPLACE it; a failure-style
        # reason captures a real co-cause and is PRESERVED with the
        # alignment cause appended.
        upstream_admitted = False
        if acceptance_results:
            acceptance_passed = all(r.passed for r in acceptance_results)
            is_winning = (
                execution_succeeded
                and acceptance_passed
                and trades_aligned
                and exit_rule_conformance_passed
            )
            upstream_admitted = acceptance_passed
        elif walk_forward_failed and execution_succeeded:
            # Walk-forward fallback: anomaly recheck occurs after refinement
            # in the verification phase.
            fallback_anomalies = self.anomaly_detector.check(
                metrics,
                trades,
                dsr_aware=False,
                coverage_report=metrics.coverage_report,
                phase="verification",
            )
            fallback_criticals = [
                g for g in fallback_anomalies if not g.passed and g.severity == "critical"
            ]
            return_ok = metrics.annualized_return_pct > WINNING_THRESHOLD
            is_winning = (
                return_ok
                and not fallback_criticals
                and trades_aligned
                and exit_rule_conformance_passed
            )
            upstream_admitted = return_ok and not fallback_criticals
            if fallback_criticals:
                # Surface the upgraded severities so the persisted
                # gate-result history reflects the true rejection reason.
                self.record_gates(
                    fallback_anomalies, all_gate_results, gate_name_prefix="fallback_"
                )
            # Gap 7 / 9: mirror the fallback gate's own verdict onto
            # ``acceptance_reason`` so consumers don't have to grep
            # ``quality_gate_results`` for ``fallback_`` prefixes to see
            # why publication was admitted or rejected. The augmentation
            # block below uses ``upstream_admitted`` to decide whether
            # to replace this message (alignment vetoes a "passed"
            # verdict) or to append (both gates fired their own reasons).
            if upstream_admitted:
                metrics = metrics.model_copy(
                    update={
                        "acceptance_reason": "walk_forward_fallback_passed: anomaly recheck clean"
                    }
                )
            else:
                fallback_reasons: List[str] = []
                if fallback_criticals:
                    fallback_reasons.append("; ".join(g.details for g in fallback_criticals))
                if not return_ok:
                    fallback_reasons.append(
                        f"annualized_return {metrics.annualized_return_pct:.2f}% <= "
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
            is_winning = False
            # Gap 4 / 8: self-document why publication was blocked on
            # each else-branch entry path. Without this, the persisted
            # record shows ``is_winning=False`` with an empty
            # ``acceptance_reason``. The no-trades case is checked first
            # because it's the more proximate cause when both conditions
            # hold (a run with no trades cannot publish regardless of
            # ``walk_forward_enabled``). Execution-failure paths are
            # handled by the elif-narrative branch in Phase 3 instead.
            if execution_succeeded and not trades:
                metrics = metrics.model_copy(
                    update={"acceptance_reason": "publication_disabled: no trades produced"}
                )
            elif execution_succeeded and trades and not config.walk_forward_enabled:
                metrics = metrics.model_copy(
                    update={"acceptance_reason": "publication_disabled: walk_forward_enabled=False"}
                )

        # ── Publication vetoes ────────────────────────────────────────
        # Surface each veto's cause on ``acceptance_reason`` so the
        # audit trail explains why publication was blocked even when
        # the upstream acceptance gate otherwise passed. Vetoes stack:
        # the first to fire is treated as the upstream rejection by the
        # second, so a multi-gate failure preserves both causes.
        #
        # Helper :func:`_apply_veto_to_acceptance_reason` codifies the
        # "replace stale success, append real rejection" rule so adding
        # a future veto is one new call here rather than another copy
        # of the mutation shape.
        #
        # Each veto's ``execution_succeeded and trades`` precondition
        # ensures the alignment / conformance verdicts are meaningful —
        # both gates need a non-empty trade ledger to evaluate.
        # ``alignment_reports`` is in the alignment guard so we only
        # attribute the rejection to alignment when the audit actually
        # ran (skipped when ``market_data is None``).

        # Issue #527 — conformance veto.
        if execution_succeeded and trades and not exit_rule_conformance_passed:
            conformance_criticals = [
                r
                for r in all_gate_results
                if r.gate_name == "exit_rule_conformance"
                and not r.passed
                and r.severity == "critical"
            ]
            detail = "; ".join(r.details for r in conformance_criticals)
            suffix = (
                f"exit_rule_conformance_failed: {detail}"
                if detail
                else "exit_rule_conformance_failed: engine enforcement leaked"
            )
            metrics, upstream_admitted = _apply_veto_to_acceptance_reason(
                metrics, suffix, upstream_admitted=upstream_admitted
            )

        # Issue #529 — alignment veto.
        if execution_succeeded and trades and alignment_reports and not trades_aligned:
            last_report = alignment_reports[-1]
            # NOTE: do NOT name this ``rationale`` — that shadows the
            # strategy-rationale string bound earlier from the ideation
            # agent (and used positionally when calling
            # ``self.analysis_agent.run`` plus persisted on
            # ``StrategyLabRecord.strategy_rationale``). Shadowing would
            # silently corrupt both the analysis prompt and the audit
            # record on every alignment-failure path.
            align_rationale = (last_report.rationale or "").strip()
            suffix = (
                f"alignment_failed: {align_rationale}"
                if align_rationale
                else "alignment_failed: trades did not implement strategy spec"
            )
            metrics, upstream_admitted = _apply_veto_to_acceptance_reason(
                metrics, suffix, upstream_admitted=upstream_admitted
            )

        # ── Phase 3: ANALYSIS ─────────────────────────────────────────
        narrative = ""
        if execution_succeeded and trades:
            emit("analyzing", {"sub_phase": "draft"})
            try:

                def _on_analysis_sub(sub: str) -> None:
                    emit("analyzing", {"sub_phase": sub})

                narrative = self.analysis_agent.run(
                    spec,
                    metrics,
                    trades,
                    rationale,
                    on_sub_phase=_on_analysis_sub,
                    is_winning=is_winning,
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

        # #547 item 7: queryable cap-exhaustion status. The evaluation-phase
        # site sets ``execution_succeeded=True`` ("anomalous but code is
        # correct"), so without this branch those cycles would silently
        # report ``status="completed"`` despite never reaching a clean
        # backtest. With the strict reading, all three exhaustion sites now
        # flip status to ``failed: max_refinement_rounds``.
        if max_rounds_exhausted:
            backtest_status = "failed: max_refinement_rounds"
        elif execution_succeeded:
            backtest_status = "completed"
        else:
            backtest_status = "failed"

        backtest_id = f"bt-{uuid.uuid4().hex[:8]}"
        backtest_record = BacktestRecord(
            backtest_id=backtest_id,
            strategy_id=spec.strategy_id,
            strategy=spec,
            config=config,
            submitted_by="strategy_lab_v2",
            submitted_at=now_iso,
            completed_at=now_iso,
            status=backtest_status,
            result=metrics,
            trades=trades,
            requested_symbols=requested_symbols,
            fetched_symbols=fetched_symbols,
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
            original_spec=original_spec,
            original_code=original_code,
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
        """Call the alignment agent with retries on transient errors.

        On ``AlignmentAuditError`` (LLM transport / JSON parse failure),
        retries up to ``STRATEGY_LAB_ALIGNMENT_RETRIES`` times (default 2),
        then falls **closed** with ``aligned=False`` so the orchestrator's
        ``no_proposed_fix`` exit fires and a misaligned strategy whose
        audit happens to throw is not silently waved through (issue #531).
        The rationale captures the underlying error for the audit trail.
        """
        try:
            retries = max(int(os.environ.get("STRATEGY_LAB_ALIGNMENT_RETRIES", "2")), 0)
        except ValueError:
            retries = 2

        last_exc: Optional[Exception] = None
        for attempt in range(retries + 1):
            try:
                return self.alignment_agent.run(
                    spec=spec,
                    code=code,
                    trades=trades,
                    metrics=metrics,
                    prior_attempts=prior_attempts,
                )
            except AlignmentAuditError as exc:
                last_exc = exc
                logger.warning(
                    "Alignment audit attempt %d/%d failed: %s",
                    attempt + 1,
                    retries + 1,
                    exc,
                )
            except Exception as exc:
                # Unexpected error from the agent (not a transport / parse
                # failure). Don't retry — surface immediately, fail closed.
                logger.exception("Alignment agent raised unexpected error; failing closed")
                return TradeAlignmentReport(
                    aligned=False,
                    proposed_code=None,
                    rationale=(f"Alignment audit error (fail-closed): {type(exc).__name__}: {exc}"),
                )

        # All retries exhausted — emit a terminal ERROR so ops alerting
        # rules keyed on ERROR-level logs still fire (the per-attempt
        # WARNING above is intentionally low-severity for transient
        # hiccups).
        assert last_exc is not None, "loop ran at least once; last_exc must be set"
        logger.error(
            "Alignment audit error after %d attempts; failing closed: %s",
            retries + 1,
            last_exc,
        )
        return TradeAlignmentReport(
            aligned=False,
            proposed_code=None,
            rationale=(
                f"Alignment audit error after {retries + 1} attempts (fail-closed): "
                f"{type(last_exc).__name__}: {last_exc}"
            ),
        )


    def _apply_updates(
        self,
        spec: StrategySpec,
        updates: Dict[str, Any],
        code: str,
        failure_phase: Optional[str] = None,
    ) -> StrategySpec:
        """Apply refinement updates: code-only, with risk-limits tighten-only carve-out (#543).

        The spec (entry/exit/sizing rules, hypothesis) is immutable
        post-ideation; ``risk_limits`` may only be tightened. Stray
        spec-mutating keys are logged and discarded. Repeated stray
        emissions on the same ``failure_phase`` (≥ ``_SPEC_MUTATION_TRIP_THRESHOLD``
        in a row) raise ``SpecImplementabilityError`` so the orchestrator
        can re-route to ideation. Any attempted ``risk_limits`` loosening
        also raises immediately.
        """
        data = spec.model_dump()
        data["strategy_code"] = code

        stray = set(updates) - _REFINEMENT_ALLOWED_KEYS - _REFINEMENT_PASSTHROUGH_KEYS
        # ``risk_limits: null`` is treated as "no change requested" — skip the
        # tighten-only merge entirely. Because the key is in
        # ``_REFINEMENT_PASSTHROUGH_KEYS`` it is also not counted as stray.
        risk_limits_proposed = updates.get("risk_limits")

        if risk_limits_proposed is not None:
            merged_limits, loosened, unknown = _merge_risk_limits_tighten_only(
                spec.risk_limits, risk_limits_proposed
            )
            if loosened:
                raise SpecImplementabilityError(
                    evidence=(f"refinement tried to loosen risk_limits fields: {sorted(loosened)}"),
                    failure_phase=failure_phase,
                    last_spec=spec,
                    last_code=code,
                )
            if unknown:
                logger.warning(
                    "Refinement proposed unknown/immutable risk_limits keys %s; "
                    "discarding for failure_phase=%s",
                    sorted(unknown),
                    failure_phase,
                )
            data["risk_limits"] = merged_limits.model_dump()

        if stray:
            logger.warning(
                "Refinement discarded spec-mutating keys %s for failure_phase=%s "
                "(refinement is code-only post-#543).",
                sorted(stray),
                failure_phase,
            )
            if failure_phase is not None:
                counter = self._consecutive_spec_mutation_rounds
                counter[failure_phase] = counter.get(failure_phase, 0) + 1
                # Reset all other phases — threshold is consecutive within
                # a single failure_phase, not interleaved.
                for other in list(counter):
                    if other != failure_phase:
                        counter[other] = 0
                if counter[failure_phase] >= _SPEC_MUTATION_TRIP_THRESHOLD:
                    raise SpecImplementabilityError(
                        evidence=(
                            f"refinement repeatedly attempted spec mutations on "
                            f"failure_phase={failure_phase}: keys={sorted(stray)} "
                            f"(round {counter[failure_phase]} of consecutive mutation attempts)"
                        ),
                        failure_phase=failure_phase,
                        last_spec=spec,
                        last_code=code,
                    )
        elif failure_phase is not None:
            # Clean refinement round on this phase — reset its counter
            # so future stray emissions start fresh.
            self._consecutive_spec_mutation_rounds[failure_phase] = 0

        return StrategySpec.model_validate(data)

    def _build_short_circuit_record(
        self,
        *,
        spec: StrategySpec,
        config: BacktestConfig,
        code: str,
        original_spec: StrategySpec,
        original_code: str,
        rationale: str,
        all_gate_results: List[QualityGateResult],
        refinement_attempts: List[str],
        short_circuit_status: str,
        short_circuit_reason: str,
        emit: PhaseCallback,
    ) -> StrategyLabRecord:
        """Persist a failed cycle that exited before code execution.

        Used by the pre-synthesis spec gate (#547 item 1) so that
        critical spec failures short-circuit without ever running
        ``run_strategy_code`` or fetching market data. The record still
        flows through ``convergence_tracker`` so failed specs influence
        diversity directives on the next cycle.
        """
        now_iso = datetime.now(timezone.utc).isoformat()

        # Mirror the short-circuit cause onto ``acceptance_reason`` so the
        # persisted record self-documents — consistent with the Gap 4/8/7/9
        # audit-trail messages set on the main publication path (#529).
        # ``short_circuit_status`` carries a ``"failed: <cause>"`` prefix at
        # every current call site; strip it so the resulting field reads
        # ``"publication_disabled: <cause>"``.
        short_circuit_metrics = compute_metrics(
            [], config.initial_capital, config.start_date, config.end_date
        )
        status_suffix = short_circuit_status.removeprefix("failed: ") or short_circuit_status
        short_circuit_metrics = short_circuit_metrics.model_copy(
            update={"acceptance_reason": f"publication_disabled: {status_suffix}"}
        )

        backtest_record = BacktestRecord(
            backtest_id=f"bt-{uuid.uuid4().hex[:8]}",
            strategy_id=spec.strategy_id,
            strategy=spec,
            config=config,
            submitted_by="strategy_lab_v2",
            submitted_at=now_iso,
            completed_at=now_iso,
            status=short_circuit_status,
            result=short_circuit_metrics,
            trades=[],
        )

        lab_record_id = f"lab-{uuid.uuid4().hex[:8]}"
        record = StrategyLabRecord(
            lab_record_id=lab_record_id,
            strategy=spec,
            backtest=backtest_record,
            is_winning=False,
            strategy_rationale=rationale,
            analysis_narrative=short_circuit_reason,
            created_at=now_iso,
            refinement_rounds=len(refinement_attempts),
            quality_gate_results=[g.model_dump() for g in all_gate_results],
            strategy_code=code,
            original_spec=original_spec,
            original_code=original_code,
        )

        self.convergence_tracker.record(spec, all_gate_results)

        emit(
            "complete",
            {
                "record_id": lab_record_id,
                "is_winning": False,
                "metrics": backtest_record.result.model_dump(),
                "refinement_rounds": len(refinement_attempts),
                "short_circuit": short_circuit_status,
            },
        )

        return record

    def _fetch_market_data(
        self,
        spec: StrategySpec,
        config: BacktestConfig,
    ) -> _MarketDataFetch:
        """Fetch OHLCV data for the strategy's asset class.

        Issue #376 — when the strategy spec carries an
        ``audit.data_snapshot_id``, treat it as the ``as_of`` cutoff so
        a re-run of the saved spec replays the exact same snapshot.
        Specs without it use ``None`` (latest).

        Issue #525 — returns a ``_MarketDataFetch`` envelope so the
        caller can record the requested-vs-fetched symbol audit trail on
        ``BacktestRecord``. ``data`` is ``None`` when nothing usable came
        back; ``requested_symbols`` is always populated (even on
        exception) so the audit trail records intent.
        """
        # Issue #523 — honour explicit target_symbols verbatim; fall back
        # to the asset-class default universe (capped by
        # STRATEGY_LAB_MAX_UNIVERSE_SYMBOLS, default 10) otherwise.
        try:
            requested = self.market_data_service.resolve_strategy_symbols(spec)
        except Exception:
            logger.exception("Symbol resolution failed for %s", spec.strategy_id)
            return _MarketDataFetch(data=None, requested_symbols=[], fetched_symbols=[])
        if not requested:
            return _MarketDataFetch(data=None, requested_symbols=[], fetched_symbols=[])
        try:
            as_of = (getattr(spec, "audit", None) and spec.audit.data_snapshot_id) or None
            data = self.market_data_service.fetch_multi_symbol_range(
                symbols=requested,
                asset_class=spec.asset_class,
                start_date=config.start_date,
                end_date=config.end_date,
                as_of=as_of,
            )
        except Exception:
            logger.exception("Market data fetch failed for %s", spec.asset_class)
            return _MarketDataFetch(
                data=None,
                requested_symbols=list(requested),
                fetched_symbols=[],
            )
        fetched = sorted(sym for sym, bars in (data or {}).items() if bars)
        return _MarketDataFetch(
            data=data if data else None,
            requested_symbols=list(requested),
            fetched_symbols=fetched,
        )

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
        oos_returns = _daily_returns_from_trades(
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
            strategy_returns = _equity_to_returns(curve.equity)

            bench_dates, bench_equity = self._build_benchmark_equity(spec, market_data, config)
            if len(bench_equity) < 2:
                return []
            benchmark_returns = _equity_to_returns(bench_equity)

            n = min(len(strategy_returns), len(benchmark_returns))
            strategy_returns = strategy_returns[:n]
            benchmark_returns = benchmark_returns[:n]
            aligned_dates = list(bench_dates[: n + 1])  # equity has n+1 points; returns has n

            subwindows = vix_quartile_subwindows(
                aligned_dates,
                benchmark_returns,
                vix_provider=_resolve_vix_provider(),
            )
            return regime_comparison(strategy_returns, benchmark_returns, subwindows)
        except Exception:
            logger.exception("Regime evaluation failed for %s", spec.strategy_id)
            return []


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
                spy_dates = [_parse_bar_date(b.date) for b in spy_bars]
                spy_equity = _closes_to_equity(
                    [b.close for b in spy_bars], config.initial_capital
                )
                agg_equity = _closes_to_equity(
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
            dates = [_parse_bar_date(b.date) for b in bars]
            equity = _closes_to_equity([b.close for b in bars], config.initial_capital)
            n = min(len(dates), len(equity))
            return dates[:n], equity[:n]
        return [], []




# ──────────────────────────────────────────────────────────────────────────
# Pure helpers (formerly @staticmethod on StrategyLabOrchestrator). Each is
# stateless — moved to module-level so the orchestrator class body reflects
# coordination state only.
# ──────────────────────────────────────────────────────────────────────────

def _merge_risk_limits_tighten_only(
    current: RiskLimits, proposed: Any
) -> Tuple[RiskLimits, List[str], List[str]]:
    """Tighten-only merge of refinement-proposed risk limits (#543).

    Returns ``(merged_limits, loosened_fields, discarded_unknown_keys)``.

    - ``loosened_fields`` lists fields whose proposed value would loosen
      the limit (raise an "lower"-direction cap, lower a "higher"-direction
      floor, or transition ``target_annual_vol`` from ``None`` to a
      value — which fundamentally changes the sizing model and is
      treated as loosening).
    - ``discarded_unknown_keys`` lists fields the caller proposed that
      either aren't in the ``RiskLimits`` schema or are marked
      immutable in ``_RISK_LIMIT_TIGHTEN_DIRECTION`` (e.g.
      ``vol_lookback_days``).

    Callers raise ``SpecImplementabilityError`` when ``loosened_fields``
    is non-empty; unknown keys are warned but never trip.
    """
    loosened: List[str] = []
    unknown: List[str] = []
    if not isinstance(proposed, dict):
        return current, loosened, unknown

    merged_data = current.model_dump()
    for key, new_value in proposed.items():
        direction = _RISK_LIMIT_TIGHTEN_DIRECTION.get(key)
        if direction is None:
            # Either unknown to RiskLimits or explicitly immutable.
            unknown.append(key)
            continue

        current_value = merged_data.get(key)

        # Special-case ``target_annual_vol``: ``None`` means "no vol
        # target" (flat sizing). Switching to a value or vice-versa
        # changes the sizing model — treat any None↔value transition
        # as loosening.
        if key == "target_annual_vol":
            if current_value is None and new_value is not None:
                loosened.append(key)
                continue
            if current_value is not None and new_value is None:
                loosened.append(key)
                continue

        try:
            cmp_current = float(current_value) if current_value is not None else None
            cmp_new = float(new_value) if new_value is not None else None
        except (TypeError, ValueError):
            unknown.append(key)
            continue

        if cmp_current is None or cmp_new is None:
            # Already handled above; defensive.
            continue

        if direction == "lower":
            if cmp_new < cmp_current:
                merged_data[key] = new_value
            elif cmp_new > cmp_current:
                loosened.append(key)
            # equal: no-op
        elif direction == "higher":
            if cmp_new > cmp_current:
                merged_data[key] = new_value
            elif cmp_new < cmp_current:
                loosened.append(key)
            # equal: no-op

    try:
        merged = RiskLimits.model_validate(merged_data)
    except Exception:
        # Validation failed on the merged limits — bail out without
        # mutating; surface every proposed key as unknown so the caller
        # logs the full set and keeps the original limits.
        logger.warning(
            "Refined risk_limits failed pydantic validation; keeping current limits unchanged."
        )
        return current, loosened, sorted(set(unknown) | set(proposed.keys()))

    return merged, loosened, unknown

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

def _equity_to_returns(equity: Sequence[float]) -> List[float]:
    out: List[float] = []
    for i in range(1, len(equity)):
        prev = equity[i - 1]
        if prev <= 0:
            out.append(0.0)
        else:
            out.append((equity[i] - prev) / prev)
    return out

def _closes_to_equity(closes: Sequence[float], initial_capital: float) -> List[float]:
    if not closes or closes[0] <= 0:
        return []
    scale = initial_capital / closes[0]
    return [c * scale for c in closes]

def _parse_bar_date(d: str) -> Any:
    from datetime import date

    return date.fromisoformat(d[:10])

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
