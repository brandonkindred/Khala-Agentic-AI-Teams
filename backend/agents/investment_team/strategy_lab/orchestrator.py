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
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple

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
from ..trading_service.modes.sandbox_compat import StrategyRunResult, run_strategy_code
from .agents.alignment import (
    AlignmentAuditError,
    AlignmentIssue,
    TradeAlignmentAgent,
    TradeAlignmentReport,
)
from .agents.analysis import AnalysisAgent, format_misalignment_prefix
from .agents.ideation import IdeationAgent
from .agents.refinement import RefinementAgent
from .agents.zero_trade_repair import ZeroTradeRepairAgent
from .coverage_probe import format_coverage_report
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


def _resolve_alignment_report_for_analysis(
    alignment_reports: List[TradeAlignmentReport],
    *,
    exit_rule_conformance_passed: bool,
) -> Optional[TradeAlignmentReport]:
    """Pick the alignment report fed into the analysis prompts.

    Returns the most recent report from the alignment loop unless the
    deterministic ``ExitRuleConformanceGate`` then vetoed publication after
    the LLM audit had cleared the run — in which case a synthetic
    misaligned report is substituted so the analysis prompt can't narrate
    "audit clean" over the conformance veto (#532).

    Returns ``None`` when the alignment loop never ran (no reports).
    """
    if not alignment_reports:
        return None
    latest = alignment_reports[-1]
    if latest.aligned and not exit_rule_conformance_passed:
        return TradeAlignmentReport(
            aligned=False,
            rationale=(
                "ExitRuleConformanceGate vetoed publication: the LLM "
                "alignment audit returned aligned=True, but the "
                "deterministic conformance check then flagged "
                "engine-enforced exit-rule violations in the final ledger "
                "that the audit had missed."
            ),
            issues=[
                AlignmentIssue(
                    rule_type="exit_rules",
                    severity="critical",
                    description=(
                        "ExitRuleConformanceGate failed: structured exit "
                        "rules did not fire as required on at least one "
                        "trade. Treat the executed trades as not a valid "
                        "test of the spec."
                    ),
                )
            ],
        )
    return latest


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

    def _run_synthesis_loop(
        self,
        *,
        spec: StrategySpec,
        code: str,
        config: BacktestConfig,
        all_gate_results: List[QualityGateResult],
        refinement_attempts: List[str],
        zero_trade_attempts: List[str],
        emit: PhaseCallback,
    ) -> _SynthesisLoopOutcome:
        """Run up to ``MAX_CODE_REFINEMENT_ROUNDS`` of (validate → fetch →
        execute → trade-collect → evaluate), refining ``spec``/``code``
        between rounds.

        Pre: pre-synthesis spec gating already passed (the caller's
        ``_run_pre_synthesis_phase`` returned ``None``); ``all_gate_results``
        is the running gate list the loop appends to via ``record_gates``;
        ``refinement_attempts`` and ``zero_trade_attempts`` are the running
        change-log lists the loop appends to in-place.
        Post: returns a ``_SynthesisLoopOutcome`` carrying the final
        ``spec``/``code``/``trades``/``metrics`` (plus ``market_data`` and
        the universe audit lists), with ``execution_succeeded=True`` iff
        a round produced a clean run with no critical anomalies, and
        ``max_rounds_exhausted=True`` iff the loop ran the full budget
        without converging. The two flags are mutually exclusive. The
        loop never raises — fatal failures short-circuit by setting flags
        and returning.

        State mutations on the caller's lists (``all_gate_results``,
        ``refinement_attempts``, ``zero_trade_attempts``) happen in-place
        and the caller observes them directly; the outcome dataclass
        carries only values the caller cannot read off shared mutable
        state.
        """
        assert isinstance(spec, StrategySpec), "spec must be a StrategySpec"
        assert isinstance(code, str), "code must be a string"
        assert isinstance(config, BacktestConfig), "config must be a BacktestConfig"
        assert isinstance(all_gate_results, list), "all_gate_results must be a list"
        assert isinstance(refinement_attempts, list), "refinement_attempts must be a list"
        assert isinstance(zero_trade_attempts, list), "zero_trade_attempts must be a list"

        trades: List[TradeRecord] = []
        metrics = compute_metrics(
            [], config.initial_capital, config.start_date, config.end_date
        )
        execution_succeeded = False
        market_data: Optional[Dict[str, List[OHLCVBar]]] = None
        requested_symbols: List[str] = []
        fetched_symbols: List[str] = []
        max_rounds_exhausted = False

        for round_num in range(MAX_CODE_REFINEMENT_ROUNDS):
            round_gate_results: List[QualityGateResult] = []

            # ── 2a: VALIDATE (code safety + spec readiness on round 0) ───
            emit("coding", {"sub_phase": "started", "refinement_round": round_num})
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
                failure_details = "\n".join(
                    f"- [{g.gate_name}] {g.details}" for g in critical_failures
                )
                spec, code, exhausted = self._refine_or_exhaust(
                    spec=spec,
                    code=code,
                    failure_phase="validation",
                    failure_details=failure_details,
                    metrics=None,
                    refinement_attempts=refinement_attempts,
                    round_num=round_num,
                    default_change_label="validation fix",
                    emit=emit,
                )
                if exhausted:
                    max_rounds_exhausted = True
                    break
                continue

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
                failure_details = (
                    f"Error type: {exec_result.error_type}\n"
                    f"stderr:\n{exec_result.stderr[:2000]}"
                )
                spec, code, exhausted = self._refine_or_exhaust(
                    spec=spec,
                    code=code,
                    failure_phase="execution",
                    failure_details=failure_details,
                    metrics=None,
                    refinement_attempts=refinement_attempts,
                    round_num=round_num,
                    default_change_label="execution fix",
                    emit=emit,
                )
                if exhausted:
                    max_rounds_exhausted = True
                    break
                continue

            # ── 2d: COLLECT TRADES + target-symbol coverage on trades ─
            trades = exec_result.trades

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

            # ── 2e: BACKTEST EVALUATION (anomaly gates → zero-trade-repair → generic refine) ─
            metrics = compute_metrics(
                trades, config.initial_capital, config.start_date, config.end_date
            )

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
                recovery = self._handle_critical_anomalies(
                    spec=spec,
                    code=code,
                    trades=trades,
                    metrics=metrics,
                    exec_result=exec_result,
                    market_data=market_data,
                    config=config,
                    critical_anomalies=critical_anomalies,
                    all_gate_results=all_gate_results,
                    refinement_attempts=refinement_attempts,
                    zero_trade_attempts=zero_trade_attempts,
                    round_num=round_num,
                    emit=emit,
                )
                spec, code = recovery.spec, recovery.code
                trades, metrics = recovery.trades, recovery.metrics
                exec_result = recovery.exec_result
                if recovery.exhausted:
                    # Even if the code is technically correct, the cycle
                    # exhausted its rounds on an unresolved anomaly. Leaving
                    # execution_succeeded=False ensures is_winning stays False
                    # so paper-trading does not fire on a
                    # "failed: max_refinement_rounds" record.
                    max_rounds_exhausted = True
                    break
                continue

            # All gates passed — code is clean and backtest is sound
            execution_succeeded = True
            break

        # Post-condition: success and round-exhaustion are mutually exclusive.
        assert not (execution_succeeded and max_rounds_exhausted), (
            "synthesis loop returned both execution_succeeded and max_rounds_exhausted"
        )
        return _SynthesisLoopOutcome(
            spec=spec,
            code=code,
            trades=trades,
            metrics=metrics,
            market_data=market_data,
            requested_symbols=requested_symbols,
            fetched_symbols=fetched_symbols,
            execution_succeeded=execution_succeeded,
            max_rounds_exhausted=max_rounds_exhausted,
        )

    def _handle_critical_anomalies(
        self,
        *,
        spec: StrategySpec,
        code: str,
        trades: List[TradeRecord],
        metrics: BacktestResult,
        exec_result: StrategyRunResult,
        market_data: Dict[str, List[OHLCVBar]],
        config: BacktestConfig,
        critical_anomalies: List[QualityGateResult],
        all_gate_results: List[QualityGateResult],
        refinement_attempts: List[str],
        zero_trade_attempts: List[str],
        round_num: int,
        emit: PhaseCallback,
    ) -> _AnomalyRecoveryOutcome:
        """Recover from critical backtest anomalies in the evaluation phase.

        Pre: ``critical_anomalies`` is non-empty; the caller has already
        run the anomaly detector and recorded its gates;
        ``all_gate_results``, ``refinement_attempts``, ``zero_trade_attempts``
        are running lists the helper mutates in place.
        Post: returns an ``_AnomalyRecoveryOutcome``. On ``exhausted=False``
        the spec/code/trades/metrics/exec_result fields carry the new
        known-good state (either a committed zero-trade-repair proposal or
        the source the generic refinement loop produced) and the caller
        ``continue``s the synthesis loop. On ``exhausted=True`` the round
        budget is spent and the caller breaks with
        ``max_rounds_exhausted=True``.

        Strategy:
          1. If diagnostics carry a ``zero_trade_category`` AND there is
             market data, ask the specialised repair agent first. A
             committed proposal has already cleared safety + fresh
             backtest + anomaly gates, so we use it directly.
          2. Otherwise (or if the repair did not commit), fall through
             to the generic refinement agent via ``_refine_or_exhaust``.
        """
        assert critical_anomalies, "_handle_critical_anomalies requires at least one critical"
        assert isinstance(market_data, dict) and market_data, "market_data must be non-empty"

        # ── 1: Build the failure-details prompt block (also used by generic refine) ──
        failure_details = "\n".join(f"- {g.details}" for g in critical_anomalies)
        diagnostics_block = _format_execution_diagnostics(exec_result.execution_diagnostics)
        if diagnostics_block:
            failure_details = f"{failure_details}\n{diagnostics_block}"
        coverage_block = format_coverage_report(metrics.coverage_report)
        if coverage_block:
            failure_details = f"{failure_details}\n{coverage_block}"

        # ── 2: Specialised zero-trade repair (if diagnostics support it) ──
        diag = exec_result.execution_diagnostics
        if diag is not None and diag.zero_trade_category is not None:
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
                assert zt_outcome.new_spec is not None, "committed ZTR must carry new_spec"
                assert zt_outcome.new_metrics is not None, "committed ZTR must carry new_metrics"
                assert zt_outcome.new_exec_result is not None, "committed ZTR must carry new_exec_result"
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
                        "changes_made": (zt_outcome.changes_made or "zero-trade repair"),
                        "via": "zero_trade_repair",
                    },
                )
                return _AnomalyRecoveryOutcome(
                    spec=zt_outcome.new_spec,
                    code=zt_outcome.new_code,
                    trades=zt_outcome.new_trades,
                    metrics=zt_outcome.new_metrics,
                    exec_result=zt_outcome.new_exec_result,
                    exhausted=False,
                )

        # ── 3: Generic refinement (or exhaust the round budget) ──
        new_spec, new_code, exhausted = self._refine_or_exhaust(
            spec=spec,
            code=code,
            failure_phase="evaluation",
            refine_label="evaluation (backtest anomaly)",
            failure_details=failure_details,
            metrics=metrics,
            refinement_attempts=refinement_attempts,
            round_num=round_num,
            default_change_label="anomaly fix",
            emit=emit,
        )
        return _AnomalyRecoveryOutcome(
            spec=new_spec,
            code=new_code,
            trades=trades,
            metrics=metrics,
            exec_result=exec_result,
            exhausted=exhausted,
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
            )
            spec, code = round_outcome.spec, round_outcome.code
            trades, metrics = round_outcome.trades, round_outcome.metrics
            if round_outcome.terminate:
                break

        return _AlignmentLoopOutcome(
            spec=spec,
            code=code,
            trades=trades,
            metrics=metrics,
            alignment_attempts=alignment_attempts,
            alignment_reports=alignment_reports,
            trades_aligned=bool(alignment_reports and alignment_reports[-1].aligned),
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
        assert align_round >= 0, "align_round must be non-negative"
        assert isinstance(market_data, dict) and market_data, "market_data must be non-empty"

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

        def _terminate() -> _AlignmentRoundOutcome:
            return _AlignmentRoundOutcome(
                spec=spec, code=code, trades=trades, metrics=metrics, terminate=True
            )

        if report.aligned:
            emit("aligning", {"sub_phase": "aligned", "alignment_round": align_round})
            return _terminate()

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
            return _terminate()

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
            return _terminate()

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
        critical_safety = [g for g in safety_gates if not g.passed and g.severity == "critical"]
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
            return _terminate()

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
            return _terminate()

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
            return _terminate()

        # All gates passed — commit the proposal as the new known-good state.
        alignment_attempts.append(change_summary)
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
        )

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

        The three is_winning branches mirror the orchestrator's three
        publication-decision paths:
          * Walk-forward succeeded → acceptance_gate verdict
          * Walk-forward raised → anomaly recheck with ``dsr_aware=False``
          * Walk-forward disabled / no trades → ``is_winning=False``
        """
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

        # Issue #527 — deterministic check that the engine enforced
        # ``spec.exit_rules`` against the FINAL trade ledger
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

        # Resolve is_winning across the three publication-decision paths.
        # ``upstream_admitted`` records whether the upstream gate (walk-
        # forward or fallback) said admit. It feeds the veto-augmentation
        # block below: a success-style ``acceptance_reason`` is REPLACED
        # by the veto cause; a failure-style reason is PRESERVED with the
        # veto appended.
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
            # Self-document why publication was blocked on each else-branch
            # entry path. Without this, the persisted record shows
            # ``is_winning=False`` with an empty ``acceptance_reason``. The
            # no-trades case is checked first because it's the more
            # proximate cause when both conditions hold.
            if execution_succeeded and not trades:
                metrics = metrics.model_copy(
                    update={"acceptance_reason": "publication_disabled: no trades produced"}
                )
            elif execution_succeeded and trades and not config.walk_forward_enabled:
                metrics = metrics.model_copy(
                    update={"acceptance_reason": "publication_disabled: walk_forward_enabled=False"}
                )

        # Publication vetoes — surface each veto's cause on
        # ``acceptance_reason`` so the audit trail explains why publication
        # was blocked even when the upstream acceptance gate passed.
        # ``_apply_veto_to_acceptance_reason`` codifies the "replace stale
        # success, append real rejection" rule.
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

        if execution_succeeded and trades and alignment_reports and not trades_aligned:
            last_report = alignment_reports[-1]
            # NOTE: do NOT name this ``rationale`` — the strategy-rationale
            # string is bound by the caller's ideation result and is also
            # what gets persisted to ``StrategyLabRecord.strategy_rationale``.
            align_rationale = (last_report.rationale or "").strip()
            suffix = (
                f"alignment_failed: {align_rationale}"
                if align_rationale
                else "alignment_failed: trades did not implement strategy spec"
            )
            metrics, upstream_admitted = _apply_veto_to_acceptance_reason(
                metrics, suffix, upstream_admitted=upstream_admitted
            )

        return _VerificationOutcome(
            metrics=metrics,
            is_winning=is_winning,
            upstream_admitted=upstream_admitted,
            acceptance_results=acceptance_results,
            walk_forward_failed=walk_forward_failed,
            exit_rule_conformance_passed=exit_rule_conformance_passed,
        )

    def _run_analysis_phase(
        self,
        *,
        spec: StrategySpec,
        metrics: BacktestResult,
        trades: List[TradeRecord],
        rationale: str,
        is_winning: bool,
        execution_succeeded: bool,
        refinement_attempts: List[Dict[str, Any]],
        all_gate_results: List[QualityGateResult],
        alignment_report: Optional[TradeAlignmentReport],
        emit: PhaseCallback,
    ) -> str:
        """Run the analysis agent and return the narrative string.

        Pre: synthesis + alignment + verification have settled. The narrative
        is whatever the analysis agent produces, or a synthetic auto-summary
        when the agent raises or there were no trades to analyse.
        Post: returns a non-empty string when ``execution_succeeded and trades``
        was true (or the failure-path auto-summary). Empty string only when
        the cycle had no trades AND no execution failure to summarise — a
        state the orchestrator treats as "nothing to write about".
        """
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
                    alignment_report=alignment_report,
                )
                emit("analyzing", {"sub_phase": "completed", "is_winning": is_winning})
                return narrative
            except Exception:
                logger.exception("Analysis agent failed for %s", spec.strategy_id)
                label = "winning" if is_winning else "losing"
                summary = (
                    f"Auto-summary: {spec.asset_class} strategy ({label}) with "
                    f"annualized return {metrics.annualized_return_pct:.1f}%. "
                    f"(Detailed narrative generation failed.)"
                )
                # When the agent raises before its internal _fallback_narrative
                # path runs (model factory, prompt-file IO, etc.), the
                # misalignment disclaimer would otherwise be lost on aligned=
                # False runs (#532).
                prefix = format_misalignment_prefix(alignment_report)
                return f"{prefix}\n{summary}" if prefix else summary
        if not execution_succeeded:
            return (
                f"Strategy failed to produce valid backtest results after "
                f"{len(refinement_attempts)} refinement round(s). "
                f"Last failure: {all_gate_results[-1].details if all_gate_results else 'unknown'}."
            )
        return ""

    def _assemble_record(
        self,
        *,
        spec: StrategySpec,
        code: str,
        config: BacktestConfig,
        metrics: BacktestResult,
        trades: List[TradeRecord],
        narrative: str,
        original_spec: StrategySpec,
        original_code: str,
        rationale: str,
        requested_symbols: List[str],
        fetched_symbols: List[str],
        max_rounds_exhausted: bool,
        execution_succeeded: bool,
        is_winning: bool,
        trades_aligned: bool,
        refinement_rounds: int,
        alignment_rounds: int,
        all_gate_results: List[QualityGateResult],
        emit: PhaseCallback,
    ) -> StrategyLabRecord:
        """Build the final ``StrategyLabRecord`` from a settled cycle.

        Pre: ``spec`` / ``code`` / ``metrics`` / ``trades`` are the
        known-good post-verification state. ``narrative`` came from the
        analysis phase (or a synthetic auto-summary on failure).
        Post: a ``BacktestRecord`` + ``StrategyLabRecord`` are constructed;
        the convergence tracker is updated; a ``"complete"`` event is
        emitted; the record is returned.

        ``status`` resolution mirrors the three terminal-state branches:
          * cap exhausted → ``"failed: max_refinement_rounds"``
          * clean exit → ``"completed"``
          * everything else → ``"failed"``
        """
        now_iso = datetime.now(timezone.utc).isoformat()

        # Cap-exhaustion status: the evaluation-phase site sets
        # ``execution_succeeded=True`` ("anomalous but code is correct"),
        # so without this branch those cycles would silently report
        # ``status="completed"`` despite never reaching a clean backtest.
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
            refinement_rounds=refinement_rounds,
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
                "is_winning": is_winning,
                "metrics": metrics.model_dump(),
                "refinement_rounds": refinement_rounds,
                "alignment_rounds": alignment_rounds,
                "trades_aligned": trades_aligned,
            },
        )

        return record

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
        # ``_run_synthesis_loop`` iterates up to ``MAX_CODE_REFINEMENT_ROUNDS``
        # rounds of (validate → fetch → execute → trade-collect → evaluate)
        # and either converges (``execution_succeeded=True``) or
        # short-circuits with ``max_rounds_exhausted`` / a fatal-fetch flag.
        # The loop appends to ``all_gate_results``, ``refinement_attempts``,
        # and ``zero_trade_attempts`` in-place; the returned outcome carries
        # the final spec/code/trades/metrics + universe audit.
        synthesis = self._run_synthesis_loop(
            spec=spec,
            code=code,
            config=config,
            all_gate_results=all_gate_results,
            refinement_attempts=refinement_attempts,
            zero_trade_attempts=zero_trade_attempts,
            emit=emit,
        )
        spec = synthesis.spec
        code = synthesis.code
        trades = synthesis.trades
        metrics = synthesis.metrics
        market_data = synthesis.market_data
        requested_symbols = synthesis.requested_symbols
        fetched_symbols = synthesis.fetched_symbols
        execution_succeeded = synthesis.execution_succeeded
        max_rounds_exhausted = synthesis.max_rounds_exhausted

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

        # ── Phase 2.7: WALK-FORWARD + ACCEPTANCE + CONFORMANCE + is_winning ────
        verification = self._run_verification_phase(
            spec=spec,
            trades=trades,
            metrics=metrics,
            market_data=market_data,
            config=config,
            execution_succeeded=execution_succeeded,
            trades_aligned=trades_aligned,
            alignment_reports=alignment_reports,
            all_gate_results=all_gate_results,
            emit=emit,
        )
        metrics = verification.metrics
        is_winning = verification.is_winning
        # The other ``_VerificationOutcome`` fields (acceptance_results,
        # walk_forward_failed, upstream_admitted) are unused beyond this
        # point — the verification phase already extended
        # ``all_gate_results`` and mutated ``metrics.acceptance_reason`` to
        # carry every downstream-visible signal. ``exit_rule_conformance_passed``
        # is consumed inside ``_resolve_alignment_report_for_analysis`` to
        # override the alignment report when the deterministic conformance
        # gate vetoes a clean LLM audit (#532).
        # ── Phase 3: ANALYSIS ─────────────────────────────────────────
        latest_alignment_report = _resolve_alignment_report_for_analysis(
            alignment_reports,
            exit_rule_conformance_passed=verification.exit_rule_conformance_passed,
        )
        narrative = self._run_analysis_phase(
            spec=spec,
            metrics=metrics,
            trades=trades,
            rationale=rationale,
            is_winning=is_winning,
            execution_succeeded=execution_succeeded,
            refinement_attempts=refinement_attempts,
            all_gate_results=all_gate_results,
            alignment_report=latest_alignment_report,
            emit=emit,
        )

        # ── Phase 4: RECORD ───────────────────────────────────────────
        return self._assemble_record(
            spec=spec,
            code=code,
            config=config,
            metrics=metrics,
            trades=trades,
            narrative=narrative,
            original_spec=original_spec,
            original_code=original_code,
            rationale=rationale,
            requested_symbols=requested_symbols,
            fetched_symbols=fetched_symbols,
            max_rounds_exhausted=max_rounds_exhausted,
            execution_succeeded=execution_succeeded,
            is_winning=is_winning,
            trades_aligned=trades_aligned,
            refinement_rounds=len(refinement_attempts),
            alignment_rounds=alignment_rounds,
            all_gate_results=all_gate_results,
            emit=emit,
        )

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

    def _refine_or_exhaust(
        self,
        *,
        spec: StrategySpec,
        code: str,
        failure_phase: str,
        failure_details: str,
        metrics: Optional[BacktestResult],
        refinement_attempts: List[str],
        round_num: int,
        default_change_label: str,
        emit: PhaseCallback,
        refine_label: Optional[str] = None,
    ) -> tuple[StrategySpec, str, bool]:
        """Apply one refinement attempt or exhaust the round budget.

        Pre: ``round_num`` is the current 0-indexed loop iteration;
        ``refinement_attempts`` is the running change-log the caller persists.
        Post: returns ``(new_spec, new_code, exhausted)``. When
        ``exhausted=False`` the caller should ``continue`` (refinement was
        applied and ``refinement_attempts`` was appended in-place); when
        ``exhausted=True`` the caller should ``break`` (no state mutated
        beyond a warning log).

        ``refine_label`` overrides ``failure_phase`` for the ``_refine``
        call only — used by the evaluation phase which passes
        ``"evaluation (backtest anomaly)"`` to the refinement LLM while
        emitting ``"evaluation"`` to the event stream.
        """
        assert isinstance(spec, StrategySpec), "spec must be a StrategySpec"
        assert isinstance(code, str), "code must be a string"
        assert isinstance(failure_phase, str) and failure_phase, "failure_phase must be non-empty"
        assert round_num >= 0, "round_num must be non-negative"

        if round_num >= MAX_CODE_REFINEMENT_ROUNDS - 1:
            logger.warning(
                "Max code refinement rounds reached on %s for %s",
                failure_phase,
                spec.strategy_id,
            )
            return spec, code, True

        emit(
            "coding",
            {
                "sub_phase": "refining",
                "refinement_round": round_num,
                "failure_phase": failure_phase,
            },
        )
        updates, new_code = self._refine(
            spec,
            code,
            refine_label or failure_phase,
            failure_details,
            metrics,
            refinement_attempts,
        )
        new_spec = self._apply_updates(spec, updates, new_code, failure_phase=failure_phase)
        changes = updates.get("changes_made", default_change_label)
        refinement_attempts.append(changes)
        emit(
            "coding",
            {
                "sub_phase": "refined",
                "refinement_round": round_num,
                "changes_made": changes,
            },
        )
        return new_spec, new_code, False

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
# Re-exports — these symbols live in :mod:`_orchestrator_helpers`. The
# orchestrator module re-exports them so existing call sites that import
# from ``investment_team.strategy_lab.orchestrator`` keep working without
# the helpers cluttering this file.
# ──────────────────────────────────────────────────────────────────────────
from ._orchestrator_helpers import (  # noqa: E402  — keep at file end
    _AlignmentLoopOutcome,
    _AlignmentRoundOutcome,
    _AnomalyRecoveryOutcome,
    _apply_veto_to_acceptance_reason,
    _closes_to_equity,
    _daily_returns_from_trades,
    _equity_to_returns,
    _format_execution_diagnostics,
    _MarketDataFetch,
    _maybe_attach_coverage_report,
    _merge_risk_limits_tighten_only,
    _parse_bar_date,
    _resolve_vix_provider,
    _SynthesisLoopOutcome,
    _VerificationOutcome,
)
