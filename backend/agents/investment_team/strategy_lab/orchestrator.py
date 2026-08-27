"""Strategy Lab Orchestrator — deterministic pipeline for code-generation backtesting.

Pipeline:
1. Strands Agent ideates strategy + generates Python code
2. Code refinement loop (up to 50 rounds): validate spec & code safety,
   execute in sandbox, fix syntax/build/runtime errors until the code
   runs cleanly and produces valid trade output
3. Backtest evaluation: compute metrics and check for anomalies
4. Strands Agent generates post-backtest narrative

Module layout — ``StrategyLabOrchestrator`` used to be a single ~3500-line
class; it is now composed from mixins, each a verbatim, behavior-preserving
extraction of one cohesive pipeline cluster:

- ``orchestrator_design.py`` (``DesignMixin``) — the design <-> review loop.
- ``orchestrator_synthesis.py`` (``SynthesisMixin``) — the code-synthesis /
  refinement loop and anomaly recovery.
- ``orchestrator_alignment.py`` (``AlignmentMixin``) — the trade-alignment
  audit/fix loop.
- ``orchestrator_verification.py`` (``VerificationMixin``) — the
  verification phase and publication-veto decisions.
- ``orchestrator_record_assembly.py`` (``RecordAssemblyMixin``) — building
  the final ``StrategyLabRecord`` (and its short-circuit variant).
- ``_orchestrator_helpers.py`` — pure helpers and outcome dataclasses
  shared by this file and all five mixins above; nothing in it depends on
  ``StrategyLabOrchestrator`` or any mixin.

This file keeps ``StrategyLabOrchestrator.__init__``, ``run_cycle`` (the
pipeline entrypoint), and any helper consumed by two or more mixins (market
data fetch, refinement/alignment merge helpers, benchmark/regime
calculations), plus whatever else each mixin's own module docstring lists
as staying on the base class. Every extracted module re-exports, in a
labeled block near the end of this file, any module-level symbol external
callers historically imported via ``investment_team.strategy_lab.orchestrator``
— check those blocks (and the mixin's module docstring) before assuming a
symbol lives here.

Per-design-attempt cross-cluster orchestration lives in
``orchestrator_design.py``'s ``_orchestrate_refinement_and_alignment`` /
``_orchestrate_verification_and_analysis`` / ``_extract_findings_and_assemble_record``
— moved there because their sole caller, ``_run_design_attempt``, already
lives in that cluster. See ``MIXIN_BOUNDARIES.md`` for the full audit.
"""

from __future__ import annotations

import logging
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
    BacktestExecutionDiagnostics,
    BacktestResult,
    CoverageReport,
    StrategyLabRecord,
    StrategySpec,
    TradeRecord,
    get_fee_defaults,
)
from ..signal_intelligence_models import SignalIntelligenceBriefV1
from ..trade_simulator import compute_metrics

# ``run_strategy_code`` has no direct caller left in this module (its sole use
# moved to ``SynthesisMixin``), but must stay bound in this module's namespace:
# tests monkeypatch it via
# ``monkeypatch.setattr(orchestrator_module, "run_strategy_code", ...)``, which
# ``SynthesisMixin``'s deferred ``from . import orchestrator`` import reads live
# at call time. Removing this import would break those tests with AttributeError.
from ..trading_service.modes.sandbox_compat import (  # noqa: F401
    StrategyRunResult,
    run_strategy_code,
)
from .agents._llm_budget import (
    DesignBudgetExhausted,
    LLMCallBudget,
    _annotate_budget_exhaustion,
    use_budget,
)
from .agents.alignment import (
    AlignmentAuditError,
    TradeAlignmentAgent,
    TradeAlignmentReport,
    findings_to_issues,
    synthesize_aligned_report,
)
from .agents.analysis import AnalysisAgent, format_misalignment_prefix
from .agents.code_synthesis import CodeSynthesisAgent, CodeSynthesisError
from .agents.design import DesignAgent
from .agents.design_review import DesignReviewAgent
from .agents.refinement import (
    _ALLOWED_OUTPUT_KEYS as _REFINEMENT_ALLOWED_KEYS,
)
from .agents.refinement import (
    _PASSTHROUGH_FOR_ORCHESTRATOR as _REFINEMENT_PASSTHROUGH_KEYS,
)
from .agents.refinement import (
    RefinementAgent,
)
from .agents.zero_trade_repair import ZeroTradeRepairAgent
from .alignment_findings import AlignmentFinding
from .budget_config import StrategyLabBudgetConfig
from .cycle_control import gather_convergence_directives, require_short_circuit_inputs
from .exceptions import SpecImplementabilityError
from .market_regime import RegimeSummary, compute_regime_summary
from .orchestrator_alignment import AlignmentMixin
from .orchestrator_design import DesignMixin
from .orchestrator_record_assembly import RecordAssemblyMixin
from .orchestrator_synthesis import SynthesisMixin
from .orchestrator_verification import VerificationMixin
from .phases import hash_code, hash_metrics_and_trades
from .quality_gates.acceptance_gate import AcceptanceGate
from .quality_gates.alignment_checks import DeterministicAlignmentChecker
from .quality_gates.backtest_anomaly import BacktestAnomalyDetector
from .quality_gates.code_conformance.gate import CodeConformanceGate
from .quality_gates.code_safety import CodeSafetyChecker
from .quality_gates.convergence_tracker import ConvergenceTracker
from .quality_gates.cost_stress_realism import CostStressRealismGate
from .quality_gates.models import QualityGateResult, StrategyLabPhase
from .quality_gates.predicate_conformance import PredicateConformanceGate, _code_conformance_retries
from .quality_gates.predicate_reachability import PredicateReachabilityProbe
from .quality_gates.realism import (
    LiquidityRealismGate,
    RegimeCoverageGate,
    RuleFiringRateGate,
    TradeClusteringGate,
)
from .quality_gates.spec_readiness import SpecReadinessGate
from .quality_gates.strategy_validator import StrategySpecValidator
from .quality_gates.target_symbol_coverage import TargetSymbolCoverageGate
from .synthesis import CompilerError, compile_strategy
from .zero_trade_repair import ZeroTradeRepairer

logger = logging.getLogger(__name__)

PhaseCallback = Callable[[str, Dict[str, Any]], None]


# Refinement output is code-only post-#543. Anything else the LLM emits is
# logged + discarded by ``_apply_updates``; ``risk_limits`` is the lone
# exception, handled with tighten-only semantics.
#
# ``RefinementAgent`` enforces the same contract on its side via
# ``_ALLOWED_OUTPUT_KEYS`` / ``_PASSTHROUGH_FOR_ORCHESTRATOR`` in
# ``agents/refinement.py``; agent-side narrowing is a first line of defense,
# orchestrator-side narrowing (using these same aliases) is authoritative.
# Imported rather than redefined so the two layers cannot drift apart.

# Threshold (per ``failure_phase``) at which repeated spec-mutation attempts
# from the refinement agent trip ``SpecImplementabilityError`` and route the
# cycle back to ideation.
_SPEC_MUTATION_TRIP_THRESHOLD = 3

# Outer-loop cap on how many times ``run_cycle`` re-enters the design
# phase after a ``SpecImplementabilityError``. ``MAX_DESIGN_REENTRIES = 2``
# permits the original design attempt + 2 re-attempts before short-circuiting.
MAX_DESIGN_REENTRIES = 2


def _refinement_stall_rounds() -> int:
    """Resolve the code-refinement loop's within-loop stall threshold.

    Pre: env value, when set, parses to ``int``.
    Post: returns a positive integer ``n`` such that the code-refinement
    loop (``_run_synthesis_loop``) short-circuits once the
    ``(hash(code), hash(failure_details))`` signature is unchanged for
    ``n`` consecutive rounds. Reads ``STRATEGY_LAB_REFINEMENT_STALL_ROUNDS``
    (default 3; sub-1 values floored to 1; garbage values fall back to 3) —
    same shape as ``_design_review_stall_rounds``, applied to the code
    -refinement loop instead of the design ↔ review loop. The stall break
    surfaces as ``status="failed: refinement_stalled"``, distinct from
    honest round-cap exhaustion (``"failed: max_refinement_rounds"``).
    """
    return StrategyLabBudgetConfig.from_env().refinement_stall_rounds


def _design_max_llm_calls() -> int:
    """Resolve the per-cycle design-phase LLM-call budget.

    Pre: env value, when set, parses to ``int``.
    Post: returns a positive integer cap on the total number of LLM calls
    the design phase may make within a single ``run_cycle`` (spanning all
    ``MAX_DESIGN_REENTRIES`` re-entries). Reads
    ``STRATEGY_LAB_DESIGN_MAX_LLM_CALLS`` (default 120, sub-1 values floored
    to 1, garbage values fall back to 120). Exhaustion short-circuits the
    cycle with ``status="failed: budget_exhausted"`` before runaway cloud
    spend rather than burning the full multiplicative worst case.
    """
    return StrategyLabBudgetConfig.from_env().design_max_llm_calls


MAX_CODE_REFINEMENT_ROUNDS = StrategyLabBudgetConfig.from_env().max_code_refinement_rounds
# ``WINNING_THRESHOLD`` (the S&P-500 amortized benchmark, 8.0%) is imported
# from ``..models`` and is the single deterministic verdict floor: a valid run
# is WINNING iff ``annualized_return_pct >= WINNING_THRESHOLD``, on every path.
# The walk-forward ``AcceptanceGate``, alignment, conformance, and realism
# gates still run and record their findings (now surfaced as narrative
# caveats), but they no longer decide the label.

# Cap on `last_order_events` included in the refinement-prompt diagnostics
# block. The model already trims to 20; 10 is enough signal for the LLM to
# spot the failure pattern while keeping the JSON line under ~1 KB.
_DIAGNOSTICS_LAST_EVENTS_CAP = 10


class StrategyLabOrchestrator(
    DesignMixin, SynthesisMixin, AlignmentMixin, VerificationMixin, RecordAssemblyMixin
):
    """Deterministic pipeline controller for the Strategy Lab.

    NOT a Strands Agent — the flow is fixed and quality gates must not be skippable.
    Strands Agents are used internally for LLM-powered steps (ideation, refinement,
    analysis).
    """

    def __init__(self, convergence_tracker: Optional[ConvergenceTracker] = None):
        # Design phase is now two LLM agents (spec author + spec reviewer)
        # plus a code-synthesis agent invoked only when the deterministic
        # compiler cannot produce code for the approved spec. The split
        # prevents the legacy "spec drifts to fit broken code" dynamic.
        self.design_agent = DesignAgent()
        self.design_review_agent = DesignReviewAgent()
        self.code_synthesis_agent = CodeSynthesisAgent()
        self.refinement_agent = RefinementAgent()
        self.alignment_agent = TradeAlignmentAgent()
        self.deterministic_alignment_checker = DeterministicAlignmentChecker()
        self.zero_trade_repair_agent = ZeroTradeRepairAgent()
        self.analysis_agent = AnalysisAgent()
        self.strategy_validator = StrategySpecValidator()
        self.code_safety_checker = CodeSafetyChecker()
        self.code_conformance_gate = CodeConformanceGate()
        self.predicate_conformance_gate = PredicateConformanceGate()
        self.anomaly_detector = BacktestAnomalyDetector()
        self.acceptance_gate = AcceptanceGate()
        self.target_symbol_coverage_gate = TargetSymbolCoverageGate()
        self.cost_stress_realism_gate = CostStressRealismGate()
        self.liquidity_realism_gate = LiquidityRealismGate()
        self.regime_coverage_gate = RegimeCoverageGate()
        self.trade_clustering_gate = TradeClusteringGate()
        self.rule_firing_rate_gate = RuleFiringRateGate()
        self.predicate_reachability_probe = PredicateReachabilityProbe()
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
        if not isinstance(symbol, str) or not symbol:
            raise ValueError("symbol must be a non-empty str")
        if not isinstance(asset_class, str) or not asset_class:
            raise ValueError("asset_class must be a non-empty str")
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

    def _compute_regime_summary(self) -> Optional[RegimeSummary]:
        """Derive the current market-regime summary for the designer prompt.

        Gated by ``STRATEGY_LAB_REGIME_SUMMARY_ENABLED`` (default ``true``;
        truthy ``"true"``/``"1"``/``"yes"``, case-insensitive). Fetches through
        the orchestrator's live :class:`MarketDataService` — the same service
        the readiness / realism gates use — so the regime read is consistent
        with the data the gates size against. The content-hashed market-data
        cache makes the small fixed set of benchmark fetches cheap across a
        batch of cycles.

        Pre: ``self.market_data_service`` is constructed.
        Post: returns a :class:`RegimeSummary` (possibly degraded) or ``None``
        when the feature is disabled. Never raises — ``compute_regime_summary``
        is itself fail-open, and any unexpected error here degrades to ``None``
        rather than crashing the cycle.
        """
        if not _env_flag("STRATEGY_LAB_REGIME_SUMMARY_ENABLED"):
            return None
        try:
            return compute_regime_summary(
                self.market_data_service.fetch_ohlcv,
                computed_at=datetime.now(timezone.utc).isoformat(),
            )
        except Exception as exc:  # noqa: BLE001 — regime input is best-effort
            logger.debug("regime summary computation failed: %s", exc)
            return None

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
        if not isinstance(results, list) or not all(
            isinstance(g, QualityGateResult) for g in results
        ):
            raise ValueError("results must be a list of QualityGateResult")
        for g in results:
            if refinement_round is not None:
                g.refinement_round = refinement_round
            if gate_name_prefix:
                g.gate_name = f"{gate_name_prefix}{g.gate_name}"
        if all_gate_results is not None:
            all_gate_results.extend(results)
        return results

    def _committed_code_conformance_verdict(
        self,
        code: str,
        spec: StrategySpec,
        *,
        all_gate_results: List[QualityGateResult],
        refinement_round: int,
        gate_name_prefix: str,
    ) -> bool:
        """Re-check predicate conformance on code committed *after* synthesis.

        The post-synthesis commit paths — trade-alignment fixes
        (``_run_alignment_round``) and zero-trade repair
        (``_handle_critical_anomalies``) — replace the persisted ``code`` /
        ``trades`` but do not otherwise re-run the predicate-conformance gate.
        This re-runs it on the committed ``code`` / ``spec`` so the
        ``ran_on_non_conforming_code`` flag describes the code that produced the
        persisted backtest.

        Pre: ``code`` / ``spec`` are a committed proposal that already executed
        a real backtest; there is no further refinement round to repair drift,
        so the gate runs at the demotion threshold (``attempt =
        _code_conformance_retries()``) — any drift surfaces as a warning.
        Post: appends the gate results to ``all_gate_results`` (with
        ``gate_name_prefix``) and returns ``True`` iff the committed code is a
        demotion-warning non-conformance. The verdict is computed *before*
        ``record_gates`` prefixes ``gate_name`` (``_round_demoted_conformance``
        matches the unprefixed ``"predicate_conformance"``). Compiled /
        no-predicate specs return a passing "skipped" result and never flag.
        """
        results = self.predicate_conformance_gate.check(
            code, spec, phase="verification", attempt=_code_conformance_retries()
        )
        verdict = _round_demoted_conformance(results)
        self.record_gates(
            results,
            all_gate_results,
            refinement_round=refinement_round,
            gate_name_prefix=gate_name_prefix,
        )
        return verdict

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
        if not name:
            raise ValueError("gate name must be non-empty")
        if not details:
            raise ValueError("details must be non-empty")
        return QualityGateResult(
            gate_name=name,
            phase=phase,
            passed=severity == "info",
            severity=severity,
            details=details,
            refinement_round=refinement_round,
        )

    def _check_anomalies_cached(
        self,
        metrics: BacktestResult,
        trades: List[TradeRecord],
        *,
        dsr_aware: bool,
        diagnostics: Optional[BacktestExecutionDiagnostics],
        coverage_report: Optional[CoverageReport],
        market_data: Optional[Dict[str, List[OHLCVBar]]],
        phase: StrategyLabPhase,
    ) -> List[QualityGateResult]:
        """Run ``anomaly_detector.check``, reusing the last result in this
        design attempt when ``(metrics, trades)`` is unchanged.

        Mirrors the pre-backtest reachability probe's signature guard
        (``reachability_sig`` in ``_run_synthesis_loop``), but the cache must
        outlive a single loop: the synthesis loop and the trade-alignment
        loop both call this within the same design attempt, so the memo is
        an attempt-scoped instance attribute (``self._last_anomaly_check``,
        reset in ``_run_design_attempt``) rather than a local variable.
        ``dsr_aware`` and ``market_data`` are fixed for the attempt's
        lifetime and ``diagnostics``/``coverage_report`` are attached onto
        ``metrics`` by the caller before this runs, so ``(metrics, trades)``
        alone determines whether ``check()``'s output would differ.

        Pre: none beyond the type constraints above — ``_run_design_attempt``
        resets ``self._last_anomaly_check`` to ``None`` at the start of every
        attempt, and a lazy ``getattr`` default handles any caller that
        invokes this before an attempt has run (e.g. a test exercising a
        round/proposal-evaluation helper directly).
        Post: returns a list of ``QualityGateResult`` objects distinct from
        (never a shared reference with) whatever is cached or was returned
        to any earlier caller — safe for the caller's ``record_gates`` to
        mutate in place (stamp ``refinement_round``, prefix ``gate_name``)
        without corrupting the cache or any already-recorded result. The
        returned gates' ``phase`` matches this call's ``phase`` regardless
        of which call originally computed them, so verdicts and metadata
        are identical to always calling ``anomaly_detector.check`` directly.
        """
        signature = hash_metrics_and_trades(metrics, trades)
        cached = getattr(self, "_last_anomaly_check", None)
        if cached is not None and cached[0] == signature:
            return [g.model_copy(update={"phase": phase}) for g in cached[1]]
        results = self.anomaly_detector.check(
            metrics,
            trades,
            dsr_aware=dsr_aware,
            diagnostics=diagnostics,
            coverage_report=coverage_report,
            phase=phase,
            market_data=market_data,
        )
        self._last_anomaly_check = (signature, [g.model_copy() for g in results])
        return [g.model_copy() for g in results]

    def run_cycle(
        self,
        prior_records: List[StrategyLabRecord],
        config: BacktestConfig,
        signal_briefs: Optional[Dict[str, SignalIntelligenceBriefV1]] = None,
        on_phase: Optional[PhaseCallback] = None,
        exclude_asset_classes: Optional[List[str]] = None,
    ) -> StrategyLabRecord:
        """Run one full strategy lab cycle, enumerated as exactly four phases.

        The pipeline exposes the four named phases in :data:`PHASES`:

            1. ``DESIGN`` — initial :class:`DesignAgent` invocation.
            2. ``DESIGN_REVIEW`` — bounded design ↔ review loop gated by
               ``SpecReadinessGate``; advances to synthesis only when the
               gate passes and the reviewer marks the spec ready.
            3. ``CODE_SYNTHESIS`` — deterministic compile or LLM synthesis
               + refinement loop gated by ``CodeConformanceGate``; advances
               to verification only when synthesis converges
               (``execution_succeeded=True``), which structurally requires
               conformance to have passed.
            4. ``BACKTEST_AND_VERIFICATION`` — trade alignment loop,
               walk-forward, acceptance gate, ``is_winning`` resolution,
               and the post-backtest analysis narrative.

        Each phase exit fires a :class:`PhaseTransition` event through the
        ``on_phase`` callback (event name :data:`PHASE_TRANSITION_EVENT_NAME`)
        carrying SHA-256 hashes of the spec and code at that boundary, so
        consumers can detect upstream-artefact drift.

        Preconditions:
          - ``prior_records`` is the (possibly empty) sequence of previously
            persisted ``StrategyLabRecord`` rows.
          - ``config`` is a constructed :class:`BacktestConfig`.
          - ``signal_briefs``, when given, maps canonical asset-class label
            (e.g. ``"stocks"``, ``"crypto"``) to the
            :class:`SignalIntelligenceBriefV1` computed from that category's
            prior records only (see ``_compute_signal_brief_snapshot``). A
            category with no prior records is simply absent from the map,
            not present with an empty brief. ``None`` (the default) means no
            precomputed briefs are available for this cycle; forwarded
            verbatim to ``_run_design_attempt``, which selects the pinned
            category's own entry per attempt.

        Postconditions:
          - Returns a :class:`StrategyLabRecord` with the final result.
          - Exactly four ``PhaseTransition`` events are emitted on the
            happy path; short-circuit paths emit only the prefix of
            boundaries actually reached.
          - On ``SpecImplementabilityError`` from a downstream phase,
            ``run_cycle`` wraps ``_run_design_attempt`` in an outer retry
            loop bounded by :data:`MAX_DESIGN_REENTRIES`, re-firing the
            full transition sequence with ``attempt`` incremented. On
            exhaustion, persists a short-circuit record with
            ``status='failed: spec_unimplementable'``.

        Invariants:
          - ``spec_hash`` on transitions is stable from the
            ``DESIGN_REVIEW → CODE_SYNTHESIS`` boundary onward within a
            single design attempt (spec frozen post-design).
          - ``code_hash`` on transitions is stable from the
            ``CODE_SYNTHESIS → BACKTEST_AND_VERIFICATION`` boundary onward
            within a single design attempt.
        """
        emit = on_phase or (lambda phase, data: None)

        # Gather convergence directives once — appended to on loopback.
        directives: List[str] = gather_convergence_directives(self.convergence_tracker)

        last_evidence: Optional[str] = None
        last_spec: Optional[StrategySpec] = None
        last_code: str = ""
        last_failure_phase: Optional[str] = None
        last_design_context: Optional[_DesignPersistContext] = None
        # Counts every ``SpecImplementabilityError`` raised within this
        # ``run_cycle``, including the final raise that exhausts the
        # re-entry budget. Threaded into ``_run_design_attempt`` so the
        # persisted record (success or short-circuit) reflects the
        # phase-back history, and used to advance the DSR trial counter
        # by one per phase-back: each failed attempt consumed real LLM
        # work on the same evaluation window and so contributes to the
        # multiple-testing burden that DSR deflation corrects for.
        phase_back_count: int = 0
        # Parent commit log for drift across attempts. Each design attempt
        # works on its own clean child collector (copy-on-entry); the child is
        # merged back here only once the attempt's fate is known
        # (commit-on-completion). This keeps a failed attempt's spec/code
        # revisions out of the next attempt's working state while still
        # preserving them for the short-circuit diagnostic record. See
        # ``RETRY_STATE_ISOLATION.md``.
        drift_collector = _DriftCollector()
        cumulative_gate_results: List[QualityGateResult] = []
        # Per-cycle LLM-call budget. Bound once here via ``use_budget`` so it
        # spans every design re-entry below — the cap is a true ceiling on
        # the whole cycle, not a fresh allowance per attempt. The design
        # agents charge it through ``charge_active_budget`` at each LLM call;
        # no ``budget`` argument is threaded through the call chain.
        llm_budget = LLMCallBudget(_design_max_llm_calls())
        # Current market-regime read, computed once per cycle and shared across
        # every design re-entry so the designer conditions its setup archetype
        # on trend / volatility state (see the "Setup playbook" system prompt).
        # Fail-open: ``None`` when disabled or on data failure — the designer
        # simply omits the regime section then.
        regime_summary = self._compute_regime_summary()
        with use_budget(llm_budget):
            for design_attempt in range(MAX_DESIGN_REENTRIES + 1):
                # Copy-on-entry: hand this attempt a clean child collector so
                # drift from a prior failed attempt cannot poison it.
                attempt_drift = drift_collector.snapshot()
                try:
                    return self._run_design_attempt(
                        prior_records=prior_records,
                        config=config,
                        signal_briefs=signal_briefs,
                        emit=emit,
                        exclude_asset_classes=exclude_asset_classes,
                        directives=directives,
                        design_attempt=design_attempt,
                        phase_back_count=phase_back_count,
                        drift_collector=attempt_drift,
                        cumulative_gate_results=cumulative_gate_results,
                        regime_summary=regime_summary,
                    )
                except SpecImplementabilityError as exc:
                    last_evidence = exc.evidence
                    last_spec = exc.last_spec
                    last_code = exc.last_code
                    last_failure_phase = exc.failure_phase
                    last_design_context = exc.design_context
                    phase_back_count += 1
                    self.convergence_tracker.increment_trials(1)
                    # Commit-on-completion: fold the failed attempt's drift into
                    # the parent commit log so the short-circuit record retains
                    # its diagnostics. The next attempt's ``snapshot`` is still a
                    # fresh empty child, so this does not contaminate it.
                    drift_collector.merge(attempt_drift)
                    if design_attempt >= MAX_DESIGN_REENTRIES:
                        break
                    emit(
                        "designing",
                        {
                            "sub_phase": "loopback",
                            "design_attempt": design_attempt + 1,
                            "phase_back_count": phase_back_count,
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
        require_short_circuit_inputs(last_spec, last_evidence)
        return self._build_short_circuit_record(
            spec=last_spec,
            config=config,
            code=last_code,
            original_spec=last_spec,
            original_code=last_code,
            rationale="",
            all_gate_results=cumulative_gate_results,
            refinement_attempts=[],
            short_circuit_status="failed: spec_unimplementable",
            short_circuit_reason=(
                f"Spec unimplementable after {MAX_DESIGN_REENTRIES + 1} design attempts "
                f"(last failure_phase={last_failure_phase}): {last_evidence}"
            ),
            emit=emit,
            design_context=last_design_context,
            phase_back_count=phase_back_count,
            drift_collector=drift_collector,
        )

    def _run_realism_gates(
        self,
        *,
        spec: StrategySpec,
        trades: List[TradeRecord],
        metrics: BacktestResult,
        config: BacktestConfig,
        market_data: Optional[Dict[str, List[OHLCVBar]]],
        execution_succeeded: bool,
        open_position_entry_reasons: Optional[List[str]] = None,
        alignment_findings: Optional[List[AlignmentFinding]] = None,
    ) -> List[QualityGateResult]:
        """Run verification-phase realism gates and return their results.

        Preconditions:
          - Called from :meth:`_run_verification_phase` between walk-forward
            evaluation and the publication-veto block.
          - ``metrics`` carries the post-walk-forward backtest result.
          - ``config`` is the run's :class:`BacktestConfig`.
          - ``market_data`` is the per-symbol bar table used for the run;
            the liquidity gate self-skips when this is ``None``.
          - ``alignment_findings`` is the latest
            :class:`TradeAlignmentReport`'s ``alignment_findings`` ledger
            (``None`` when the alignment loop produced no report yet); it
            is the ``RuleFiringRateGate``'s sole rule-firing signal for a
            ``requires_custom_code=True`` spec, since that path has no
            reliable ``entry_reason``/``exit_reason`` annotation to count.
        Postconditions:
          - Returns ``[]`` when execution didn't succeed or the ledger is
            empty — the gates' contracts are only meaningful for a strategy
            that actually traded.
          - Otherwise returns one or more :class:`QualityGateResult`s; the
            caller treats any ``critical`` entry as a publication veto and
            appends every entry to the persisted gate timeline.
        Invariants:
          - Pure orchestration: never mutates ``spec`` or ``trades``.
          - Gates are run in a fixed order so the persisted timeline is
            deterministic across re-runs of the same record.
          - The cost-stress gate is invoked unconditionally; it
            self-skips (info) when ``config.cost_stress=False``, so legacy
            single-window and walk-forward-fallback paths never trip a
            spurious veto here. Enforcement of "mandatory cost-stress on
            winning-candidate runs" lives at the production entrypoint
            (``build_strategy_lab_batch_input`` force-enables the flag).
        """
        if not execution_succeeded or not trades:
            return []
        results: List[QualityGateResult] = []
        results.extend(
            self.target_symbol_coverage_gate.check_breadth(spec, trades, phase="verification")
        )
        results.extend(self.cost_stress_realism_gate.check(metrics, config, phase="verification"))
        results.extend(self.liquidity_realism_gate.check(trades, market_data, phase="verification"))
        results.extend(self.regime_coverage_gate.check(metrics, phase="verification"))
        results.extend(self.trade_clustering_gate.check(trades, phase="verification"))
        results.extend(
            self.rule_firing_rate_gate.check(
                spec,
                trades,
                open_position_entry_reasons=open_position_entry_reasons or [],
                alignment_findings=alignment_findings,
                phase="verification",
            )
        )
        return results

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
                narrative = self.analysis_agent.run(
                    spec,
                    metrics,
                    trades,
                    rationale,
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

    def _synthesize_initial_code(
        self,
        *,
        spec: StrategySpec,
        config: BacktestConfig,
        rationale: str,
        all_gate_results: List[QualityGateResult],
        design_attempt: int,
        phase_back_count: int,
        drift_collector: _DriftCollector,
        design_context: _DesignPersistContext,
        emit: PhaseCallback,
    ) -> _CodeSynthesisPhaseResult:
        """Synthesize the initial strategy code for a converged spec.

        Pre: the design loop reached readiness; ``spec`` is the converged
        candidate.
        Post: returns a ``_CodeSynthesisPhaseResult``. Deterministic compilation
        is tried first (falling back to ``requires_custom_code`` on
        ``CompilerError``); the custom-code path delegates to the synthesis
        agent and, on ``CodeSynthesisError``, returns a short-circuit ``record``
        for the caller to return. On success ``record`` is ``None``, the code
        drift is recorded, ``spec.strategy_code`` is set, the pre-refinement
        ``original_spec`` / ``original_code`` snapshot is taken, generic fee
        defaults are overridden per asset class, and the ``synthesized`` event
        is emitted.
        """
        # Deterministic compile by default; the LLM-driven synthesis
        # agent is reserved for specs that genuinely cannot be compiled
        # (``requires_custom_code=True`` or ``CompilerError``).
        code = ""
        if not spec.requires_custom_code:
            try:
                code = compile_strategy(spec)
            except CompilerError as exc:
                logger.warning(
                    "compiler_fallback strategy_id=%s reason=%s",
                    spec.strategy_id,
                    exc,
                )
                spec.requires_custom_code = True

        if not code:
            # Custom-code path: hand the frozen spec to the synthesis
            # agent. A failure here is terminal for the cycle — we will
            # not silently advance into the synthesis loop with no code.
            try:
                code = self.code_synthesis_agent.run(spec)
            except CodeSynthesisError as exc:
                abort_reason = f"Code synthesis failed after design converged: {exc}"
                emit("designing", {"sub_phase": "aborted", "reason": abort_reason})
                return _CodeSynthesisPhaseResult(
                    record=self._build_short_circuit_record(
                        spec=spec,
                        config=config,
                        code="",
                        original_spec=spec,
                        original_code="",
                        rationale=rationale,
                        all_gate_results=all_gate_results,
                        refinement_attempts=[],
                        short_circuit_status="failed: code_synthesis",
                        short_circuit_reason=abort_reason,
                        emit=emit,
                        design_context=design_context,
                        phase_back_count=phase_back_count,
                        drift_collector=drift_collector,
                    )
                )

        drift_collector.record_code_change(
            phase="synthesis",
            agent="compiler" if not spec.requires_custom_code else "CodeSynthesisAgent",
            before_code="",
            after_code=code,
            reason="initial code synthesis",
        )

        spec.strategy_code = code
        # ``original_spec`` / ``original_code`` are snapshotted after the
        # design loop converges but before the refinement loop mutates
        # anything, so reviewers can compare against any refinement-
        # driven change persisted on the final record.
        original_spec = spec.model_copy(deep=True)
        original_code = code

        # Override generic fee defaults with asset-class-appropriate values
        if config.transaction_cost_bps == 5.0 and config.slippage_bps == 2.0:
            fee_defaults = get_fee_defaults(spec.asset_class)
            config = config.model_copy(update=fee_defaults)

        emit(
            "designing",
            {
                "sub_phase": "synthesized",
                "strategy": {
                    "asset_class": spec.asset_class,
                    "hypothesis": spec.hypothesis[:120],
                    "design_rounds": design_context.rounds,
                },
            },
        )
        return _CodeSynthesisPhaseResult(
            record=None,
            code=code,
            original_spec=original_spec,
            original_code=original_code,
            config=config,
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
        except DesignBudgetExhausted:
            raise
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
        stall_tracker: RefinementStallTracker,
        refine_label: Optional[str] = None,
        drift_collector: Optional[_DriftCollector] = None,
    ) -> tuple[StrategySpec, str, bool, bool]:
        """Apply one refinement attempt or exhaust the round budget.

        Pre: ``round_num`` is the current 0-indexed loop iteration;
        ``refinement_attempts`` is the running change-log the caller persists;
        ``stall_tracker`` is scoped to one ``_run_synthesis_loop`` invocation
        (shared across all three call sites within it).
        Post: returns ``(new_spec, new_code, exhausted, stalled)``. When
        ``exhausted=False`` (``stalled`` is always ``False`` in this case)
        the caller should ``continue`` (refinement was applied and
        ``refinement_attempts`` was appended in-place); when
        ``exhausted=True`` the caller should ``break`` (no state mutated
        beyond a warning log). ``stalled=True`` means the exhaustion was
        caused by ``stall_tracker`` detecting an unchanged
        ``(code, failure_details)`` signature for consecutive rounds, rather
        than genuinely running out of rounds.

        ``refine_label`` overrides ``failure_phase`` for the ``_refine``
        call only — used by the evaluation phase which passes
        ``"evaluation (backtest anomaly)"`` to the refinement LLM while
        emitting ``"evaluation"`` to the event stream.
        """
        if not isinstance(spec, StrategySpec):
            raise TypeError(f"spec must be a StrategySpec, got {type(spec).__name__}")
        if not isinstance(code, str):
            raise TypeError(f"code must be a string, got {type(code).__name__}")
        if not isinstance(failure_phase, str) or not failure_phase:
            raise ValueError(f"failure_phase must be a non-empty string, got {failure_phase!r}")
        if round_num < 0:
            raise ValueError(f"round_num must be non-negative, got {round_num}")

        # Record this round's (code, failure) signature before deciding
        # stall/round-cap, mirroring the design-review loop's
        # record-then-check ordering (``CritiqueLedger.record_round`` then
        # ``is_stalled``). Recorded even on the final allowed round so the
        # tracker's history is always complete for diagnostics.
        stall_tracker.record(hash_code(code), hash_code(failure_details))
        stall_rounds = _refinement_stall_rounds()

        # Stall check first, and only when rounds remain — a stall trip on
        # the FINAL allowed round is round-cap exhaustion, not an early
        # abort, mirroring the design-review loop's
        # ``review_round < max_rounds - 1`` guard exactly.
        if round_num < MAX_CODE_REFINEMENT_ROUNDS - 1 and stall_tracker.is_stalled(stall_rounds):
            logger.warning(
                "Refinement stalled on %s for %s: (code, failure) signature "
                "unchanged for %d round(s)",
                failure_phase,
                spec.strategy_id,
                stall_rounds,
            )
            emit(
                "coding",
                {
                    "sub_phase": "stalled",
                    "refinement_round": round_num,
                    "failure_phase": failure_phase,
                    "stall_rounds": stall_rounds,
                },
            )
            return spec, code, True, True

        if round_num >= MAX_CODE_REFINEMENT_ROUNDS - 1:
            logger.warning(
                "Max code refinement rounds reached on %s for %s",
                failure_phase,
                spec.strategy_id,
            )
            return spec, code, True, False

        emit(
            "coding",
            {
                "sub_phase": "refining",
                "refinement_round": round_num,
                "failure_phase": failure_phase,
            },
        )
        try:
            updates, new_code = self._refine(
                spec,
                code,
                refine_label or failure_phase,
                failure_details,
                metrics,
                refinement_attempts,
            )
        except DesignBudgetExhausted as exc:
            _annotate_budget_exhaustion(exc, spec, code=code)
            raise
        try:
            new_spec = self._apply_updates(spec, updates, new_code, failure_phase=failure_phase)
        except SpecImplementabilityError as exc:
            exc.drift_collector = drift_collector
            raise
        changes = updates.get("changes_made", default_change_label)
        refinement_attempts.append(changes)
        if drift_collector is not None:
            drift_collector.record_code_change(
                phase="synthesis",
                agent="RefinementAgent",
                before_code=code,
                after_code=new_code,
                reason=changes,
            )
            drift_collector.record_spec_change(
                phase="synthesis",
                agent="RefinementAgent",
                before_spec=spec,
                after_spec=new_spec,
                reason=changes,
            )
        emit(
            "coding",
            {
                "sub_phase": "refined",
                "refinement_round": round_num,
                "changes_made": changes,
            },
        )
        return new_spec, new_code, False, False

    def _run_alignment_audit(
        self,
        spec: StrategySpec,
        code: str,
        trades: List[TradeRecord],
        metrics: BacktestResult,
        prior_attempts: List[str],
        *,
        market_data: Dict[str, List[OHLCVBar]],
        config: BacktestConfig,
    ) -> Tuple[TradeAlignmentReport, List[QualityGateResult]]:
        """Run the deterministic alignment gate, then optionally the LLM fix proposer.

        Pre: ``trades`` is the executed ledger, ``market_data`` is the
        same in-memory OHLCV dictionary the sandbox consumed.
        Post: returns a ``(TradeAlignmentReport, gate_results)`` pair.
        When the gate finds ``aligned=True``, the report is synthesised
        from the deterministic findings with no LLM call. When the gate
        finds critical misalignments, the LLM ``propose_code_fix`` is
        invoked with retries; on parse-failure exhaustion the report
        falls closed (``aligned=False``, ``proposed_code=None``) so the
        loop's existing ``no_proposed_fix`` exit fires.

        ``gate_results`` is the per-rule :class:`QualityGateResult` list
        the gate emitted; the caller appends them to ``all_gate_results``
        so the dashboard sees every check that ran.
        """
        check_result = self.deterministic_alignment_checker.check(
            spec=spec,
            trades=trades,
            market_data=market_data,
            initial_capital=config.initial_capital,
            near_miss_adjudicator=self.alignment_agent.adjudicate_near_miss,
        )

        if check_result.aligned:
            report = synthesize_aligned_report(check_result.findings)
            return report, check_result.gate_results

        # Misaligned: ask the LLM for a code patch grounded in the
        # structured findings. ``propose_code_fix`` routes through the LLM
        # envelope, which owns the bounded jittered-backoff retry loop
        # (``STRATEGY_LAB_ALIGNMENT_RETRIES``) and raises
        # ``AlignmentAuditError`` only after transport/parse retries are
        # exhausted. Any failure falls closed (``aligned=False``,
        # ``proposed_code=None``) so a transient outage or an agent bug can
        # never silently produce a green audit.
        try:
            report = self.alignment_agent.propose_code_fix(
                spec=spec,
                code=code,
                findings=check_result.findings,
                prior_attempts=prior_attempts,
            )
            # The LLM might echo ``aligned=True`` on this path (the
            # fix-proposer parse keeps ``proposed_code`` via
            # ``preserve_proposed_code=True`` so an over-claim doesn't strip
            # a usable patch). The deterministic gate's verdict is
            # authoritative — clamp ``aligned`` to ``False`` so the loop
            # keeps driving.
            if report.aligned:
                report.aligned = False
            report.alignment_findings = list(check_result.findings)
            # The deterministic findings are the authoritative description of
            # what went wrong. The LLM's narrative ``issues`` may omit,
            # under-specify, or rephrase them, which would leave downstream
            # analysis prompts (``analysis.py``'s alignment-status section)
            # with nothing concrete to cite. Always re-derive ``issues`` from
            # the structured findings so the deterministic-first contract
            # holds end-to-end.
            report.issues = findings_to_issues(check_result.findings)
            return report, check_result.gate_results
        except DesignBudgetExhausted:
            raise
        except AlignmentAuditError as exc:
            # The envelope already retried transient transport faults; an
            # AlignmentAuditError here means those retries were exhausted, or
            # the LLM returned an unparseable response (parse failures run
            # after the envelope and are not retried — they fail closed).
            logger.error(
                "Alignment fix-proposer failed after envelope retries; failing closed: %s",
                exc,
            )
            exc_repr = f"{type(exc).__name__}: {exc}"
        except Exception as exc:  # noqa: BLE001 — fail closed on any unexpected fault
            logger.exception("Alignment agent raised unexpected error; failing closed")
            exc_repr = f"{type(exc).__name__}: {exc}"

        fail_closed = TradeAlignmentReport(
            aligned=False,
            proposed_code=None,
            rationale=f"Alignment fix-proposer error (fail-closed): {exc_repr}",
            issues=findings_to_issues(check_result.findings),
            alignment_findings=list(check_result.findings),
        )
        return fail_closed, check_result.gate_results

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
        # Issue #533 — snapshot ``provider_used`` for the just-fetched
        # subset only. ``MarketDataService.provider_used`` is shared
        # mutable state that accumulates across fetches; without the
        # filter+copy here a later cycle's fetch would pollute this row.
        service_provider_used = getattr(self.market_data_service, "provider_used", {}) or {}
        provider_used = {
            sym: service_provider_used[sym] for sym in fetched if sym in service_provider_used
        }
        return _MarketDataFetch(
            data=data if data else None,
            requested_symbols=list(requested),
            fetched_symbols=fetched,
            provider_used=provider_used,
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
        except Exception:  # pragma: no cover — regime-evaluation failure path defensive; live tests exercise the happy path
            logger.exception("Regime evaluation failed for %s", spec.strategy_id)
            return []

    def _cached_fetch_benchmark_bars(
        self,
        *,
        symbols: List[str],
        asset_class: str,
        start_date: str,
        end_date: str,
        as_of: Optional[str],
    ) -> Dict[str, List[OHLCVBar]]:
        """Fetch benchmark bars through an attempt-scoped memo cache.

        Preconditions:
            ``symbols`` is a non-empty list of benchmark tickers; ``self``
            may or may not have been through ``_run_design_attempt`` yet —
            the cache is created lazily so a call made directly (e.g. from a
            test, outside ``_run_design_attempt``) still works with a
            degenerate one-entry cache.

        Postconditions:
            Returns the ``fetch_multi_symbol_range`` result for
            ``(symbols, asset_class, start_date, end_date, as_of)``. The
            first call for a given key delegates to
            ``self.market_data_service.fetch_multi_symbol_range`` with
            byte-identical arguments and stores the result; subsequent calls
            with the same key return the stored result without refetching.
            A raised exception is not cached — the next call with the same
            key retries the fetch.
        """
        cache = getattr(self, "_benchmark_bars_cache", None)
        if cache is None:
            cache = self._benchmark_bars_cache = {}
        key = (tuple(symbols), asset_class, start_date, end_date, as_of)
        if key not in cache:
            cache[key] = self.market_data_service.fetch_multi_symbol_range(
                symbols=symbols,
                asset_class=asset_class,
                start_date=start_date,
                end_date=end_date,
                as_of=as_of,
            )
        return cache[key]

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
        ``config.initial_capital``. The underlying fetch is memoized via
        ``_cached_fetch_benchmark_bars`` so repeated calls within the same
        design attempt (e.g. multiple regime/walk-forward evaluations) issue
        at most one network fetch per distinct ``(symbols, window, as_of)``.
        """
        composition = (config.benchmark_composition or "").strip().lower()
        # Issue #376 — pin benchmark fetches to the same ``as_of`` as the
        # strategy fetch so a saved spec re-runs against a consistent
        # historical snapshot of both strategy bars and benchmark bars.
        as_of = (getattr(spec, "audit", None) and spec.audit.data_snapshot_id) or None
        if composition == "60_40":
            try:
                blend = self._cached_fetch_benchmark_bars(
                    symbols=["SPY", "AGG"],
                    asset_class="stocks",
                    start_date=config.start_date,
                    end_date=config.end_date,
                    as_of=as_of,
                )
            except Exception:  # pragma: no cover — benchmark fetch failure path exercised via fail-closed integration only
                logger.exception("60/40 benchmark fetch failed; falling back to single-symbol")
                blend = None
            if blend and "SPY" in blend and "AGG" in blend and blend["SPY"] and blend["AGG"]:
                spy_bars = blend["SPY"]
                agg_bars = blend["AGG"]
                spy_dates = [_parse_bar_date(b.date) for b in spy_bars]
                spy_equity = _closes_to_equity([b.close for b in spy_bars], config.initial_capital)
                agg_equity = _closes_to_equity([b.close for b in agg_bars], config.initial_capital)
                blended = build_60_40_equity(
                    spy_equity, agg_equity, initial_capital=config.initial_capital
                )
                n = min(len(spy_dates), len(blended))
                return spy_dates[:n], blended[:n]

        # Single-symbol fallback
        bench_symbol = benchmark_for_strategy(spec)
        try:
            single = self._cached_fetch_benchmark_bars(
                symbols=[bench_symbol],
                asset_class=spec.asset_class,
                start_date=config.start_date,
                end_date=config.end_date,
                as_of=as_of,
            )
        except Exception:  # pragma: no cover — single-symbol benchmark fetch failure path exercised via fail-closed integration only
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
#
# ``publishability_skip_reason`` shares its name with unrelated
# ``publishability_skip_reason: Optional[str] = None`` parameters on
# ``_extract_findings_and_assemble_record`` (``orchestrator_design.py``) and
# ``_assemble_record`` (``orchestrator_record_assembly.py``), which trips
# ruff's F811 "redefinition of unused import" check on those parameter
# definitions even though they're in a different scope and file. Each
# carries a suppression comment with the explanation.
# ──────────────────────────────────────────────────────────────────────────
from ._orchestrator_helpers import (  # noqa: E402,F401  — keep at file end
    RefinementStallTracker,
    _AlignmentLoopOutcome,
    _apply_veto_to_acceptance_reason,
    _attach_execution_diagnostics,
    _build_rule_implementation_map,
    _closes_to_equity,
    _CodeSynthesisPhaseResult,
    _daily_returns_from_trades,
    _DesignPersistContext,
    _DriftCollector,
    _emit_phase_transition,
    _env_flag,
    _equity_to_returns,
    _format_execution_diagnostics,
    _MarketDataFetch,
    _maybe_attach_coverage_report,
    _merge_risk_limits_tighten_only,
    _parse_bar_date,
    _RefinementAlignmentResult,
    _resolve_vix_provider,
    _round_demoted_conformance,
    _SynthesisLoopOutcome,
    _VerificationOutcome,
    publishability_skip_reason,
)

# ──────────────────────────────────────────────────────────────────────────
# Re-exports — these symbols live in :mod:`orchestrator_alignment`. The
# orchestrator module re-exports them so existing call sites that import
# from ``investment_team.strategy_lab.orchestrator`` keep working without
# the alignment-loop cluster cluttering this file.
# ──────────────────────────────────────────────────────────────────────────
from .orchestrator_alignment import (  # noqa: E402,F401  — keep at file end
    MAX_ALIGNMENT_ROUNDS,
    _AlignmentRoundOutcome,
)

# ──────────────────────────────────────────────────────────────────────────
# Re-exports — these symbols live in :mod:`orchestrator_design`. The
# orchestrator module re-exports them so existing call sites that import
# from ``investment_team.strategy_lab.orchestrator`` keep working without
# the design-loop helpers cluttering this file.
# ──────────────────────────────────────────────────────────────────────────
from .orchestrator_design import (  # noqa: E402,F401  — keep at file end
    _coerce_expectancy_forecast,
    _coerce_requires_custom_code,
    _critique_from_readiness,
    _demote_compilable_custom_code_enabled,
    _design_loop_telemetry_summary,
    _design_review_rounds,
    _design_review_stall_rounds,
    _DesignLoopOutcome,
    _DesignPhaseResult,
    _emit_design_review_telemetry,
    _format_regression_notice,
    _mechanical_repair_enabled,
    _resolve_alignment_report_for_analysis,
    _spec_readiness_signature,
    build_spec_from_dict,
)

# ──────────────────────────────────────────────────────────────────────────
# Re-exports — these symbols live in :mod:`orchestrator_record_assembly`.
# The orchestrator module re-exports them so existing call sites that import
# from ``investment_team.strategy_lab.orchestrator`` keep working without
# the record-assembly cluster cluttering this file.
# ──────────────────────────────────────────────────────────────────────────
from .orchestrator_record_assembly import (  # noqa: E402,F401  — keep at file end
    _finalize_loop_telemetry,
)

# ──────────────────────────────────────────────────────────────────────────
# Re-exports — these symbols live in :mod:`orchestrator_synthesis`. The
# orchestrator module re-exports them so existing call sites that import
# from ``investment_team.strategy_lab.orchestrator`` keep working without
# the synthesis-loop cluster cluttering this file.
# ──────────────────────────────────────────────────────────────────────────
from .orchestrator_synthesis import (  # noqa: E402,F401  — keep at file end
    _AnomalyRecoveryOutcome,
    _SynthesisEvaluateResult,
)
