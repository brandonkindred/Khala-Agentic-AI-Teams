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
from typing import Any, Callable, Dict, List, Literal, Optional, Set, Tuple

from pydantic import ValidationError

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
    WINNING_THRESHOLD,
    BacktestConfig,
    BacktestRecord,
    BacktestResult,
    DataProvenance,
    ExpectancyForecast,
    GateEvent,
    StrategyLabRecord,
    StrategySpec,
    TradeRecord,
    get_fee_defaults,
)
from ..signal_intelligence_models import SignalIntelligenceBriefV1
from ..strategy_lab_context import normalize_asset_class, normalize_asset_class_strict
from ..trade_simulator import compute_metrics
from ..trading_service.modes.sandbox_compat import StrategyRunResult, run_strategy_code
from .agents._llm_budget import (
    DesignBudgetExhausted,
    LLMCallBudget,
    active_budget,
    use_budget,
)
from .agents.alignment import (
    AlignmentAuditError,
    AlignmentIssue,
    TradeAlignmentAgent,
    TradeAlignmentReport,
    findings_to_issues,
    synthesize_aligned_report,
)
from .agents.analysis import AnalysisAgent, format_misalignment_prefix
from .agents.code_synthesis import CodeSynthesisAgent, CodeSynthesisError
from .agents.design import DesignAgent
from .agents.design_review import (
    CritiqueIssue,
    CritiqueLedger,
    DesignReviewAgent,
    LedgerDelta,
    SpecCritique,
)
from .agents.refinement import RefinementAgent
from .agents.zero_trade_repair import ZeroTradeRepairAgent
from .alignment_findings import AlignmentFinding
from .backtest_cache import BacktestCache
from .coverage_probe import format_coverage_report
from .exceptions import SpecImplementabilityError
from .mechanical_repair import RepairAction, repair_spec, select_code_path
from .phases import (
    PHASE_TRANSITION_EVENT_NAME,
    Phase,
    PhaseTransition,
    hash_code,
    hash_spec,
)
from .quality_gates.acceptance_gate import AcceptanceGate, summarize_acceptance_reason
from .quality_gates.alignment_checks import DeterministicAlignmentChecker
from .quality_gates.backtest_anomaly import BacktestAnomalyDetector
from .quality_gates.code_conformance import CodeConformanceGate
from .quality_gates.code_safety import CodeSafetyChecker
from .quality_gates.convergence_tracker import ConvergenceTracker
from .quality_gates.cost_stress_realism import CostStressRealismGate
from .quality_gates.exit_rule_conformance import ExitRuleConformanceGate
from .quality_gates.models import QualityGateResult, StrategyLabPhase
from .quality_gates.predicate_conformance import PredicateConformanceGate, _code_conformance_retries
from .quality_gates.realism import (
    LiquidityRealismGate,
    RegimeCoverageGate,
    RuleFiringRateGate,
    TradeClusteringGate,
)
from .quality_gates.spec_readiness import SpecReadinessGate
from .quality_gates.strategy_validator import StrategySpecValidator
from .quality_gates.target_symbol_coverage import TargetSymbolCoverageGate
from .quality_gates.universe_injection import inject_universe_and_guard
from .spec_dsl import DEFAULT_SIZING_PAYLOAD
from .synthesis import CompilerError, compile_strategy
from .zero_trade_repair import ZeroTradeRepairer

logger = logging.getLogger(__name__)

PhaseCallback = Callable[[str, Dict[str, Any]], None]


def _coerce_requires_custom_code(raw: Any) -> bool:
    """Coerce an arbitrary value to ``bool`` for ``StrategySpec.requires_custom_code``.

    Pre:  ``raw`` is any value from an untrusted JSON source.
    Post: returns a real ``bool``. Recognised truthy: ``True``, ``1``
          (int), and case-insensitive ``"true"`` / ``"yes"`` / ``"on"``
          / ``"1"`` / ``"t"`` / ``"y"``. Recognised falsey: ``False``,
          ``0`` (int), and case-insensitive ``"false"`` / ``"no"`` /
          ``"off"`` / ``"0"`` / ``"f"`` / ``"n"`` / ``""``. Everything
          else (``None``, prose, off-spec ints) → ``False``.
    Invariant: never raises. Default ``False`` keeps the deterministic
          compile path on for malformed input rather than aborting the
          attempt with a Pydantic ValidationError.
    """
    if raw is None:
        return False
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int) and not isinstance(raw, bool):
        if raw == 0:
            return False
        if raw == 1:
            return True
        return False
    if isinstance(raw, str):
        s = raw.strip().lower()
        if s in {"true", "yes", "on", "1", "t", "y"}:
            return True
        if s in {"false", "no", "off", "0", "f", "n", ""}:
            return False
    return False


def _coerce_expectancy_forecast(raw: Any) -> Optional[ExpectancyForecast]:
    """Coerce an LLM-emitted ``expectancy_forecast`` blob to ``ExpectancyForecast``.

    Pre:  ``raw`` is any value from an untrusted JSON source — typically a
          dict of the forecast fields, ``None`` when the designer omitted it,
          or an already-built ``ExpectancyForecast``.
    Post: returns an ``ExpectancyForecast`` (with values clamped by the model's
          validators) when ``raw`` is a usable dict / instance; returns
          ``None`` for a missing or unusable forecast.
    Invariant: never raises. The forecast is advisory and never gated, so a
          malformed blob degrades to ``None`` rather than aborting the cycle
          with a ValidationError.
    """
    if raw is None:
        return None
    if isinstance(raw, ExpectancyForecast):
        return raw
    if isinstance(raw, dict):
        try:
            return ExpectancyForecast(**raw)
        except (ValidationError, TypeError) as exc:
            logger.warning("DesignAgent emitted unusable expectancy_forecast (%s); dropping.", exc)
            return None
    return None


def build_spec_from_dict(strategy_dict: Dict[str, Any], *, strategy_id: str) -> "StrategySpec":
    """Construct a ``StrategySpec`` from a design-agent JSON payload.

    Pre: ``strategy_dict`` is the JSON dict returned by
    :meth:`DesignAgent.run` or :meth:`DesignAgent.revise` — already
    validated to carry structured-DSL rule shapes; ``strategy_id`` is
    stable across the design loop so revisions of the same lineage
    share an id.
    Post: returns a freshly constructed ``StrategySpec`` carrying the
    supplied ``strategy_id``. The caller is responsible for any
    subsequent mutation (compile, fee defaults).

    Accepted ``asset_class`` aliases (equity/equities/stock/etf/etfs, fx,
    commodity/metal/energy, cryptocurrency/cryptocurrencies) are
    canonicalized before construction so a clean mapping never trips the
    strict ``StrategySpec`` validator.

    A *genuinely unsupported* class the strict normalizer rejects (e.g.
    ``bonds``) is NOT silently coerced to ``stocks`` — doing so would run
    the original (bonds) hypothesis against the stock universe and stock
    gates and record it as a valid stock backtest. Instead this raises
    :class:`SpecImplementabilityError`, which ``run_cycle`` catches to
    re-enter the design phase with the defect as evidence (bounded by
    ``MAX_DESIGN_REENTRIES``); on exhaustion the cycle short-circuits with
    ``status="failed: spec_unimplementable"`` rather than a misleading
    record. This keeps the cycle alive (no unhandled ``ValidationError``
    crash) while refusing to mislabel the experiment.

    This is a free function (no orchestrator state) so it can be unit-tested
    directly; ``StrategyLabOrchestrator._build_spec_from_dict`` is a thin
    wrapper over it.

    Raises:
        SpecImplementabilityError: the payload names an unsupported
            ``asset_class`` that no alias maps to a tradeable class.
    """
    raw_asset_class = strategy_dict.get("asset_class", "stocks")
    asset_class = normalize_asset_class(raw_asset_class)
    unsupported_class = False
    try:
        normalize_asset_class_strict(raw_asset_class)
    except ValueError:
        unsupported_class = True

    spec = StrategySpec(
        strategy_id=strategy_id,
        authored_by="strategy_lab_v2",
        asset_class=asset_class,
        hypothesis=strategy_dict.get("hypothesis", ""),
        signal_definition=strategy_dict.get("signal_definition", ""),
        timeframe=strategy_dict.get("timeframe") or "1d",
        entry_rules=strategy_dict.get("entry_rules", []),
        exit_rules=strategy_dict.get("exit_rules", []),
        sizing=strategy_dict.get("sizing", DEFAULT_SIZING_PAYLOAD),
        target_symbols=strategy_dict.get("target_symbols", []),
        risk_limits=strategy_dict.get("risk_limits", {}),
        speculative=strategy_dict.get("speculative", False),
        requires_custom_code=_coerce_requires_custom_code(
            strategy_dict.get("requires_custom_code")
        ),
        expectancy_forecast=_coerce_expectancy_forecast(strategy_dict.get("expectancy_forecast")),
        strategy_code=None,
    )
    if unsupported_class:
        # ``spec`` (coerced to ``stocks``) is passed as ``last_spec`` only so
        # the short-circuit record is well-formed; the cycle will redesign
        # rather than backtest it. The evidence names the rejected class so
        # the re-entry directive steers the LLM to a supported one.
        logger.warning(
            "DesignAgent emitted unsupported asset_class %r; routing to "
            "redesign instead of coercing to %r and backtesting as stocks.",
            raw_asset_class,
            asset_class,
        )
        raise SpecImplementabilityError(
            f"Unsupported asset_class {raw_asset_class!r}: not a tradeable "
            "class and not a known alias. Re-author the strategy for one of "
            "stocks/crypto/forex/futures/commodities.",
            failure_phase="design",
            last_spec=spec,
            last_code="",
        )
    return spec


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

# Outer-loop cap on how many times ``run_cycle`` re-enters the design
# phase after a ``SpecImplementabilityError``. ``MAX_DESIGN_REENTRIES = 2``
# permits the original design attempt + 2 re-attempts before short-circuiting.
MAX_DESIGN_REENTRIES = 2


def _design_review_rounds() -> int:
    """Resolved per call so tests can override ``STRATEGY_LAB_DESIGN_REVIEW_ROUNDS``.

    Pre: env value, when set, parses to ``int`` and is ``>= 1`` after the
    ``max(.., 1)`` clamp.
    Post: returns a positive integer cap on the number of design ↔ review
    iterations per ``_run_design_attempt``. Default 20 gives the design ↔
    review loop room to converge on specs with subtle thesis / math /
    completeness issues; a sub-1 override floors to 1 so the loop runs at
    least once.
    """
    raw = os.environ.get("STRATEGY_LAB_DESIGN_REVIEW_ROUNDS", "20")
    try:
        return max(int(raw), 1)
    except ValueError:
        return 20


def _design_review_stall_rounds() -> int:
    """Resolve the within-loop stall threshold (consecutive unchanged rounds).

    Pre: env value, when set, parses to ``int``.
    Post: returns a positive integer ``n`` such that the design ↔ review loop
    short-circuits once the blocking open-issue set is non-empty and unchanged
    for ``n`` consecutive rounds. Reads ``STRATEGY_LAB_DESIGN_REVIEW_STALL_ROUNDS``
    (default 3; sub-1 values floored to 1; garbage values fall back to 3). The
    stall break is distinct from honest round-cap exhaustion — it surfaces as
    ``status="failed: design_stalled"`` so oscillation aborts are observable
    apart from specs that simply ran out of rounds.
    """
    raw = os.environ.get("STRATEGY_LAB_DESIGN_REVIEW_STALL_ROUNDS", "3")
    try:
        return max(int(raw), 1)
    except ValueError:
        return 3


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
    raw = os.environ.get("STRATEGY_LAB_DESIGN_MAX_LLM_CALLS", "120")
    try:
        return max(int(raw), 1)
    except ValueError:
        return 120


def _mechanical_repair_enabled() -> bool:
    """Resolve the deterministic mechanical-repair pre-flight toggle.

    Pre: none.
    Post: returns ``True`` unless ``STRATEGY_LAB_MECHANICAL_REPAIR_ENABLED`` is
    set to a recognised falsey value. Accepted truthy values are
    ``true``/``1``/``yes`` (case-insensitive); anything else disables the
    pre-flight and restores the pure LLM-revise behaviour. Default ``true``.
    """
    raw = os.environ.get("STRATEGY_LAB_MECHANICAL_REPAIR_ENABLED", "true")
    return raw.strip().lower() in ("true", "1", "yes")


MAX_CODE_REFINEMENT_ROUNDS = 50
# Maximum number of trade-alignment problem-solving rounds. Each round
# audits the executed trades against the spec and, if misaligned, asks the
# alignment agent to rewrite the Python code; the new code is sent back
# through the sandbox for a fresh backtest. The cap prevents runaway loops
# when the agent cannot converge.
MAX_ALIGNMENT_ROUNDS = 10
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


def _emit_phase_transition(
    emit: PhaseCallback,
    *,
    from_phase: Phase,
    to_phase: Optional[Phase],
    spec: StrategySpec,
    code: str,
    attempt: int,
) -> None:
    """Emit a :class:`PhaseTransition` event through the orchestrator callback.

    Preconditions:
      - ``emit`` is a no-op-safe ``PhaseCallback``.
      - ``from_phase`` is a member of :data:`PHASES`.
      - ``to_phase`` is the next member of :data:`PHASES` after
        ``from_phase``, or ``None`` for the terminal boundary.
      - ``spec`` is the spec as it exists at the boundary; ``code`` is
        the strategy code as it exists at the boundary (empty string
        before ``CODE_SYNTHESIS`` exits).
      - ``attempt`` is the zero-indexed ``run_cycle`` design-attempt
        counter.

    Postconditions:
      - Emits exactly one event via ``emit`` with name
        :data:`PHASE_TRANSITION_EVENT_NAME` and payload equal to
        ``PhaseTransition.model_dump(mode="json")``.
      - The payload carries SHA-256 ``spec_hash`` and ``code_hash``
        computed by :func:`hash_spec` / :func:`hash_code`.
    """
    transition = PhaseTransition(
        from_phase=from_phase,
        to_phase=to_phase,
        spec_hash=hash_spec(spec),
        code_hash=hash_code(code),
        attempt=attempt,
    )
    emit(PHASE_TRANSITION_EVENT_NAME, transition.model_dump(mode="json"))


def _spec_readiness_signature(spec: StrategySpec) -> tuple:
    """Hashable fingerprint of the spec fields :class:`SpecReadinessGate` consults.

    Pre: ``spec`` is a constructed ``StrategySpec``.
    Post: returns a tuple-of-tuples whose equality with a prior call's
    result means the readiness gate would produce the same verdict
    (modulo external state like live market data). Used by the design
    loop to skip redundant gate calls when the reviser returns the same
    readiness-relevant spec.
    """
    return (
        spec.asset_class,
        spec.timeframe,
        tuple(spec.target_symbols),
        spec.sizing.model_dump_json(),
        spec.risk_limits.model_dump_json(),
        tuple(r.model_dump_json() for r in spec.entry_rules),
        tuple(r.model_dump_json() for r in spec.exit_rules),
        # Rule 9's prose-vs-deployment check (``hypothesis:position_pct``) reads
        # ``hypothesis``, so a reviser that fixes only the prose percentage must
        # re-validate — otherwise the loop reuses a stale warning.
        spec.hypothesis,
        # Readiness depends on ``requires_custom_code`` (it gates which
        # closed-form gates apply); the Stage-2 mechanical trial-compile can flip
        # it without touching any other field, so it must be part of the
        # signature or that flip would reuse a stale (pre-flip) readiness verdict.
        bool(spec.requires_custom_code),
    )


def _critique_from_readiness(
    readiness_results: List[QualityGateResult],
) -> "SpecCritique":
    """Synthesise a :class:`SpecCritique` from deterministic readiness findings.

    Pre: ``readiness_results`` carries at least one critical or warning
    entry (the design loop only calls this helper when the deterministic
    gate failed).
    Post: returns a not-ready ``SpecCritique`` whose ``issues`` mirror the
    readiness failures so ``DesignAgent.revise`` sees the same input shape
    regardless of whether the verdict came from the LLM reviewer or a
    mechanical gate.
    """
    issues: List[CritiqueIssue] = []
    readiness_findings: List[str] = []
    for r in readiness_results:
        readiness_findings.append(f"{r.severity}: {r.details}")
        if r.passed and r.severity == "info":
            continue
        issues.append(
            CritiqueIssue(
                field="hypothesis",  # readiness covers multiple fields — surface as cross-cutting
                severity=r.severity,
                description=r.details,
                suggested_fix=(
                    "Resolve the deterministic readiness check before "
                    "the next review round can proceed."
                ),
            )
        )
    if not issues:
        # All non-info entries skipped — pathological, but make the
        # critique non-empty so the loop terminates cleanly via revise.
        issues.append(
            CritiqueIssue(
                field="hypothesis",
                severity="warning",
                description=(
                    "Readiness produced no critical finding but the "
                    "deterministic gate did not pass. Re-examine the "
                    "spec from scratch."
                ),
            )
        )
    return SpecCritique(
        ready=False,
        rationale=(
            "SpecReadinessGate identified one or more critical "
            "findings — LLM reviewer skipped this round."
        ),
        issues=issues,
        readiness_findings=readiness_findings,
    )


def _format_regression_notice(critique: "SpecCritique", regressed_ids: Set[str]) -> str:
    """Render the regression block handed to ``DesignAgent.revise``.

    Pre: ``critique`` is the round whose issues are about to be revised;
    ``regressed_ids`` is the ``LedgerDelta.regressed`` set for that round.
    Post: returns ``""`` when nothing regressed; otherwise one bullet per
    regressed issue (id, field, description) drawn from the current
    critique. The regressed ids are, by construction, present in the
    current critique's issues, so the listing is complete.
    """
    if not regressed_ids:
        return ""
    lines: List[str] = []
    for issue in critique.issues:
        if issue.issue_id in regressed_ids:
            lines.append(f"  - [{issue.issue_id}] {issue.field}: {issue.description}")
    if not lines:
        # Defensive: a regressed id with no matching issue object. List the
        # bare ids so the designer still sees the regression signal.
        lines = [f"  - {rid}" for rid in sorted(regressed_ids)]
    return "\n".join(lines)


def _emit_design_review_telemetry(
    emit: "PhaseCallback",
    review_round: int,
    ledger: CritiqueLedger,
    delta: LedgerDelta,
) -> None:
    """Emit a live per-round telemetry event on the existing callback surface.

    Pre: ``emit`` is the cycle's phase callback; ``delta`` is the ledger
    delta just produced for ``review_round``.
    Post: a single ``"telemetry"`` event is emitted carrying the running
    open-issue count and this round's resolved / regressed / new counts.
    """
    emit(
        "telemetry",
        {
            "scope": "design_review_round",
            "round": review_round,
            "open_issue_count": len(ledger.current_open),
            "resolved_count": len(delta.resolved),
            "regressed_count": len(delta.regressed),
            "new_count": len(delta.new),
        },
    )


def _design_loop_telemetry_summary(
    ledger: CritiqueLedger,
    rounds: int,
    stop_reason: str,
    mechanical_repairs: int = 0,
) -> Dict[str, Any]:
    """Build the design-loop slice of the persisted telemetry summary.

    Pre: ``ledger`` has recorded every round of the loop; ``rounds`` is the
    authoritative round count (``len(critique_history)``); ``stop_reason`` is
    one of ``"ready" | "round_cap" | "stalled" | "budget_exhausted"``;
    ``mechanical_repairs`` is the cumulative count of deterministic spec edits
    the pre-flight applied across the loop (``>= 0``).
    Post: returns the design-loop counters; gate counts and the
    compiled-vs-custom flag are merged in later by
    :meth:`StrategyLabOrchestrator._finalize_loop_telemetry`.
    """
    return {
        "design_review_rounds": rounds,
        "stop_reason": stop_reason,
        "mechanical_repairs": mechanical_repairs,
        "critique_ledger": {
            "total_resolved": len(ledger.ever_resolved),
            "total_regressed": ledger.total_regressed,
            "final_open_count": len(ledger.current_open),
        },
    }


def _round_demoted_conformance(round_gate_results: List[QualityGateResult]) -> bool:
    """Whether a single round's predicate-conformance check was demoted.

    Pre: ``round_gate_results`` are the gate results for one synthesis round.
    Post: returns ``True`` iff the round contains a ``predicate_conformance``
    result that did not pass and is a *demotion* warning (``severity ==
    "warning"``, excluding the ``"Fixture unsynthesizable:"`` "could-not-check"
    warning). Evaluated per round so the caller can attribute the verdict to the
    round whose backtest is persisted, rather than to any historical round.
    """
    return any(
        g.gate_name == "predicate_conformance"
        and not g.passed
        and g.severity == "warning"
        and not (g.details or "").startswith("Fixture unsynthesizable:")
        for g in round_gate_results
    )


def _finalize_loop_telemetry(
    design_context: "_DesignPersistContext",
    all_gate_results: List[QualityGateResult],
    spec: StrategySpec,
    code: str,
    *,
    ran_on_non_conforming_code: bool = False,
) -> Dict[str, Any]:
    """Merge the design-loop telemetry with whole-funnel gate counters.

    Pre: ``design_context`` carries the design-loop telemetry slice;
    ``all_gate_results`` is the cycle's full gate timeline; ``spec`` is the
    settled spec; ``code`` is the synthesized strategy code (empty string when
    the cycle short-circuited before code synthesis was attempted).
    ``ran_on_non_conforming_code`` is the authoritative flag captured by
    ``_run_synthesis_loop`` for the round whose backtest is persisted (defaults
    ``False`` for short-circuit records that never executed a backtest).
    Post: returns the persisted ``loop_telemetry`` summary — the design-loop
    slice plus per-gate pass/fail histograms (keyed on ``gate_name``) and a
    three-state ``code_path`` (the compiled-vs-custom share signal other
    reliability work depends on). Counts are computed once over the gate list;
    each result contributes to exactly one of pass/fail by ``passed``.

    ``code_path`` is ``"not_synthesized"`` whenever no code was produced (a
    design-phase short-circuit such as ``design_not_ready`` /
    ``design_stalled`` or an early budget exit), otherwise ``"compiled"`` /
    ``"custom"`` from ``spec.requires_custom_code``. This keeps unsynthesized
    failure records out of the compiled bucket — they have not attempted any
    compiler path, so counting the default ``requires_custom_code=False`` as
    "compiled" would corrupt the funnel metric for exactly the failures the
    telemetry exists to explain. ``requires_custom_code`` is also retained
    verbatim for backward compatibility, but it is only meaningful when
    ``code_path != "not_synthesized"``.

    ``ran_on_non_conforming_code`` is stored verbatim from the loop-captured
    flag rather than re-derived from ``all_gate_results``: the loop accumulates
    gate results across every refinement round, and the persisted backtest
    belongs to the last round that *executed and collected trades* — which is
    not necessarily the last round that ran the conformance gate (a later round
    can pass conformance yet fail execution, leaving an earlier demoted round's
    backtest in place). Only the loop knows which round's trades survived.
    """
    telemetry: Dict[str, Any] = dict(design_context.loop_telemetry)
    pass_counts: Dict[str, int] = {}
    fail_counts: Dict[str, int] = {}
    for g in all_gate_results:
        bucket = pass_counts if g.passed else fail_counts
        bucket[g.gate_name] = bucket.get(g.gate_name, 0) + 1
    telemetry["gate_pass_counts"] = pass_counts
    telemetry["gate_fail_counts"] = fail_counts
    telemetry["ran_on_non_conforming_code"] = ran_on_non_conforming_code
    requires_custom = bool(getattr(spec, "requires_custom_code", False))
    if not (code or "").strip():
        telemetry["code_path"] = "not_synthesized"
    else:
        telemetry["code_path"] = "custom" if requires_custom else "compiled"
    telemetry["requires_custom_code"] = requires_custom
    return telemetry


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
        with use_budget(llm_budget):
            for design_attempt in range(MAX_DESIGN_REENTRIES + 1):
                # Copy-on-entry: hand this attempt a clean child collector so
                # drift from a prior failed attempt cannot poison it.
                attempt_drift = drift_collector.snapshot()
                try:
                    return self._run_design_attempt(
                        prior_records=prior_records,
                        config=config,
                        signal_brief=signal_brief,
                        emit=emit,
                        exclude_asset_classes=exclude_asset_classes,
                        directives=directives,
                        design_attempt=design_attempt,
                        phase_back_count=phase_back_count,
                        drift_collector=attempt_drift,
                        cumulative_gate_results=cumulative_gate_results,
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

    def _run_design_loop(
        self,
        *,
        prior_records: List[StrategyLabRecord],
        signal_brief: Optional[SignalIntelligenceBriefV1],
        directives: List[str],
        exclude_asset_classes: Optional[List[str]],
        config: BacktestConfig,
        all_gate_results: List[QualityGateResult],
        emit: PhaseCallback,
        design_attempt: int = 0,
        drift_collector: Optional[_DriftCollector] = None,
    ) -> _DesignLoopOutcome:
        """Drive the bounded design ↔ design-review loop.

        Pre: ``self.design_agent`` and ``self.design_review_agent`` are
        constructed; ``all_gate_results`` is the orchestrator's running
        gate list (the loop appends readiness findings via
        ``self.record_gates``).
        Post: returns a :class:`_DesignLoopOutcome`. When ``ready=True``
        the caller may advance to code synthesis; when ``ready=False``
        the caller MUST short-circuit the cycle. The outcome is a value
        type — no further mutation of orchestrator state happens after
        return.

        The active design-phase budget (bound by ``use_budget`` in
        ``run_cycle``) is charged on every design/review LLM call. When it
        trips, :class:`DesignBudgetExhausted` is caught here and surfaced as
        a ``ready=False`` outcome with ``budget_exhausted=True`` carrying
        whatever spec/critique state existed at the trip — the caller maps
        this to ``status="failed: budget_exhausted"``.
        """
        max_rounds = _design_review_rounds()
        assert max_rounds >= 1, "design-review round cap must be ≥ 1"

        emit("designing", {"sub_phase": "started"})

        # Stable across the design loop so revisions of the same lineage
        # share an id — readiness gate failures, critiques, and final
        # backtest record all refer to the same ``strategy_id``.
        strategy_id = f"strat-{uuid.uuid4().hex[:8]}"

        # State referenced by the budget-exhaustion handler is initialised
        # before the LLM work so a trip on the very first ``run()`` call —
        # before ``spec`` exists — still yields a well-formed outcome. The
        # round count is derived from ``len(critique_history)`` (one critique
        # appended per round, including synthetic readiness critiques), so
        # both the success and budget-trip paths report the same number.
        spec: Optional[StrategySpec] = None
        rationale = ""
        critique_history: List[SpecCritique] = []
        ready = False
        # Owned here (not inside the review-rounds helper) so the budget-trip
        # handler below can still read the critique-ledger counters that were
        # accumulated for the rounds completed before the charge failed.
        ledger = CritiqueLedger()
        # Defaults cover the budget-trip path below (where the review-rounds
        # helper never returns its own values); the success path overwrites
        # both from the helper's return.
        stop_reason = "budget_exhausted"
        loop_telemetry: Dict[str, Any] = {}

        try:
            strategy_dict, rationale = self.design_agent.run(
                prior_records=prior_records,
                signal_brief=signal_brief,
                convergence_directives=directives or None,
                exclude_asset_classes=exclude_asset_classes,
            )
            spec = self._build_spec_from_dict(strategy_dict, strategy_id=strategy_id)

            # ═══ Phase 1 → 2 transition: DESIGN → DESIGN_REVIEW ═══════════
            # The initial DesignAgent invocation has produced a spec draft;
            # the bounded design ↔ review loop is about to start. No code
            # exists yet, so code_hash is the empty-string SHA-256.
            _emit_phase_transition(
                emit,
                from_phase=Phase.DESIGN,
                to_phase=Phase.DESIGN_REVIEW,
                spec=spec,
                code="",
                attempt=design_attempt,
            )

            spec, rationale, ready, stop_reason, loop_telemetry = self._run_design_review_rounds(
                spec=spec,
                rationale=rationale,
                strategy_id=strategy_id,
                max_rounds=max_rounds,
                config=config,
                all_gate_results=all_gate_results,
                critique_history=critique_history,
                ledger=ledger,
                emit=emit,
                drift_collector=drift_collector,
            )
        except DesignBudgetExhausted as exc:
            # Per-cycle LLM-call budget hit mid-design. Surface whatever
            # spec/critique state we reached as a not-ready outcome tagged
            # ``budget_exhausted`` so the caller short-circuits with a
            # distinct status. The tuple-return assignment above only runs on a
            # successful return, so prefer the latest in-loop spec/rationale the
            # review-rounds helper annotated on the exception (post
            # mechanical-repair / pre-trip) — otherwise the record would carry
            # the pre-loop draft even though a ``design_repair`` already fired
            # and readiness was revalidated against the repaired spec.
            latest_spec = getattr(exc, "latest_spec", None)
            if latest_spec is not None:
                spec = latest_spec
                rationale = getattr(exc, "latest_rationale", rationale)
            # ``spec`` is None only if the very first ``run()`` tripped — fall
            # back to a defaults spec so the audit record is still well-formed.
            if spec is None:
                spec = self._build_spec_from_dict({}, strategy_id=strategy_id)
            emit(
                "designing",
                {
                    "sub_phase": "budget_exhausted",
                    "calls_made": exc.calls_made,
                    "rounds": len(critique_history),
                },
            )
            # Carry forward the critique-ledger counters and the mechanical-
            # repair count accumulated for the rounds completed before the budget
            # tripped, so a budget exit after real review (or after a repair) is
            # distinguishable from one that never reached a review round.
            budget_telemetry = _design_loop_telemetry_summary(
                ledger,
                len(critique_history),
                "budget_exhausted",
                getattr(exc, "mechanical_repair_count", 0),
            )
            # Mirror the normal-exit path: emit the per-cycle ``design_loop``
            # summary so live ``on_phase`` consumers see the stop reason and
            # ledger totals on budget-exhausted cycles too, not just per-round
            # events plus the bare ``budget_exhausted`` phase event.
            emit("telemetry", {"scope": "design_loop", **budget_telemetry})
            return _DesignLoopOutcome(
                spec=spec,
                rationale=rationale,
                ready=False,
                rounds=len(critique_history),
                critique_history=critique_history,
                budget_exhausted=True,
                stop_reason="budget_exhausted",
                loop_telemetry=budget_telemetry,
            )

        return _DesignLoopOutcome(
            spec=spec,
            rationale=rationale,
            ready=ready,
            rounds=len(critique_history),
            critique_history=critique_history,
            stop_reason=stop_reason,
            loop_telemetry=loop_telemetry,
        )

    def _run_design_review_rounds(
        self,
        *,
        spec: StrategySpec,
        rationale: str,
        strategy_id: str,
        max_rounds: int,
        config: BacktestConfig,
        all_gate_results: List[QualityGateResult],
        critique_history: List["SpecCritique"],
        ledger: CritiqueLedger,
        emit: PhaseCallback,
        drift_collector: Optional[_DriftCollector],
    ) -> Tuple[StrategySpec, str, bool, str, Dict[str, Any]]:
        """Run the bounded readiness → review → revise rounds.

        Pre: ``spec`` / ``rationale`` are the initial design draft and its
        rationale; ``critique_history`` is the (empty) running list the
        caller reads back after return; ``ledger`` is the (empty)
        :class:`CritiqueLedger` the caller owns, so the budget-exhaustion
        handler in :meth:`_run_design_loop` can still read the counters
        accumulated for the rounds completed before a charge failed.
        Post: returns ``(spec, rationale, ready, stop_reason, loop_telemetry)``
        — the final candidate spec, its latest rationale, whether the
        reviewer marked it ready on the most recent round, the reason the
        loop stopped (``"ready" | "stalled" | "round_cap"``), and the
        design-loop telemetry summary. ``critique_history`` is mutated in
        place — one entry per round (synthetic readiness critiques count),
        so its length is the authoritative round count.
        May raise :class:`DesignBudgetExhausted`, which the caller handles.

        A :class:`CritiqueLedger` tracks the blocking open-issue set across
        rounds: a regressed issue (one resolved earlier that reappears) is
        surfaced to ``DesignAgent.revise`` as an explicit "do not reintroduce"
        notice, and the loop short-circuits early with ``stop_reason="stalled"``
        when the open set is unchanged for ``STRATEGY_LAB_DESIGN_REVIEW_STALL_ROUNDS``
        consecutive rounds rather than churning to the hard round cap.

        Extracted from :meth:`_run_design_loop` so the budget-exhaustion
        ``try`` there stays shallow.
        """
        ready = False
        last_readiness_signature: Optional[tuple] = None
        readiness_results: List[QualityGateResult] = []
        stall_rounds = _design_review_stall_rounds()
        stop_reason = "round_cap"
        repair_enabled = _mechanical_repair_enabled()
        mechanical_repair_count = 0

        for review_round in range(max_rounds):
            # Step 1 — deterministic readiness gate, memoized on the spec
            # signature so an unchanged spec does not re-validate.
            readiness_results, last_readiness_signature, deterministic_ready = (
                self._validate_and_memoize_readiness(
                    spec=spec,
                    config=config,
                    last_readiness_signature=last_readiness_signature,
                    readiness_results=readiness_results,
                    all_gate_results=all_gate_results,
                )
            )

            # Step 2 — deterministic mechanical pre-flight (repair criticals,
            # then trial-compile a readiness-clean spec to pick the code path)
            # before every review round.
            if repair_enabled:
                (
                    spec,
                    readiness_results,
                    last_readiness_signature,
                    deterministic_ready,
                    mechanical_repair_count,
                ) = self._run_mechanical_repair_stages(
                    spec=spec,
                    config=config,
                    readiness_results=readiness_results,
                    last_readiness_signature=last_readiness_signature,
                    deterministic_ready=deterministic_ready,
                    mechanical_repair_count=mechanical_repair_count,
                    review_round=review_round,
                    all_gate_results=all_gate_results,
                    drift_collector=drift_collector,
                    emit=emit,
                )

            # Step 3 — run the reviewer on a readiness-clean spec, else
            # synthesise a critique from the readiness findings.
            critique, delta, reviewer_ready = self._review_and_handle_critique(
                deterministic_ready=deterministic_ready,
                spec=spec,
                rationale=rationale,
                readiness_results=readiness_results,
                critique_history=critique_history,
                ledger=ledger,
                review_round=review_round,
                mechanical_repair_count=mechanical_repair_count,
                emit=emit,
            )
            if reviewer_ready:
                ready = True
                stop_reason = "ready"
                emit("designing", {"sub_phase": "ready", "rounds": len(critique_history)})
                break

            emit(
                "designing",
                {
                    "sub_phase": "round_completed",
                    "round": review_round,
                    "ready": False,
                },
            )

            # Step 4 — within-loop stall: the blocking open-issue set has been
            # non-empty and unchanged for ``stall_rounds`` rounds. Abort early
            # rather than churn to the hard round cap on a spec that is
            # oscillating instead of converging. Distinct from honest round-cap
            # exhaustion so the operator can tell them apart.
            #
            # Only treat this as a stall when there are still rounds left to
            # skip (``review_round < max_rounds - 1``). When the stall threshold
            # equals the round cap, the final allowed round trips ``is_stalled``
            # without the loop having aborted *early* — it consumed the full
            # configured budget — so it must fall through to the round-cap branch
            # and report ``round_cap`` / ``design_not_ready``.
            if review_round < max_rounds - 1 and ledger.is_stalled(stall_rounds):
                stop_reason = "stalled"
                emit(
                    "design_review",
                    {
                        "sub_phase": "stalled",
                        "round": review_round,
                        "stall_rounds": stall_rounds,
                        "open_issue_ids": sorted(ledger.current_open),
                    },
                )
                break

            if review_round >= max_rounds - 1:
                # Don't revise on the final iteration — the outer caller
                # will short-circuit using the existing critique history.
                stop_reason = "round_cap"
                break

            # Step 5 — revise the spec from this round's critique.
            spec, rationale = self._revise_with_regression_notice(
                spec=spec,
                rationale=rationale,
                critique=critique,
                delta=delta,
                critique_history=critique_history,
                strategy_id=strategy_id,
                mechanical_repair_count=mechanical_repair_count,
                drift_collector=drift_collector,
            )

        loop_telemetry = _design_loop_telemetry_summary(
            ledger, len(critique_history), stop_reason, mechanical_repair_count
        )
        emit("telemetry", {"scope": "design_loop", **loop_telemetry})
        return spec, rationale, ready, stop_reason, loop_telemetry

    def _validate_and_memoize_readiness(
        self,
        *,
        spec: StrategySpec,
        config: BacktestConfig,
        last_readiness_signature: Optional[tuple],
        readiness_results: List[QualityGateResult],
        all_gate_results: List[QualityGateResult],
    ) -> Tuple[List[QualityGateResult], Optional[tuple], bool]:
        """Run the deterministic readiness gate, memoized on the spec signature.

        Pre: ``readiness_results`` / ``last_readiness_signature`` carry the
        previous round's verdict and the spec signature that produced it.
        Post: returns ``(readiness_results, last_readiness_signature,
        deterministic_ready)``. Re-validates (and records the gates onto
        ``all_gate_results`` in place) only when the spec's readiness-relevant
        signature changed since ``last_readiness_signature`` — the gate would
        otherwise return the same verdict. ``deterministic_ready`` is ``True``
        iff no readiness critical is present.
        """
        signature = _spec_readiness_signature(spec)
        if signature != last_readiness_signature:
            readiness_results = self.spec_readiness_gate.validate(
                spec, phase="design", backtest_config=config
            )
            self.record_gates(readiness_results, all_gate_results, refinement_round=-1)
            last_readiness_signature = signature
        deterministic_ready = not any(
            (not r.passed) and r.severity == "critical" for r in readiness_results
        )
        return readiness_results, last_readiness_signature, deterministic_ready

    def _run_mechanical_repair_stages(
        self,
        *,
        spec: StrategySpec,
        config: BacktestConfig,
        readiness_results: List[QualityGateResult],
        last_readiness_signature: Optional[tuple],
        deterministic_ready: bool,
        mechanical_repair_count: int,
        review_round: int,
        all_gate_results: List[QualityGateResult],
        drift_collector: Optional[_DriftCollector],
        emit: PhaseCallback,
    ) -> Tuple[StrategySpec, List[QualityGateResult], Optional[tuple], bool, int]:
        """Run the deterministic mechanical pre-flight in two ordered stages.

        Stage 1 repairs mechanical readiness criticals (timeframe data
        availability, position-cap bound) so they never cost an LLM ``revise``
        round, then re-validates. Stage 2 — only once the spec is readiness-
        clean — trial-compiles it and flips ``requires_custom_code`` on
        ``CompilerError`` so a readiness-clean spec that is still outside the
        deterministic-compiler envelope (e.g. a ``volatility_target`` spec
        without an ATR predicate — readiness only *warns* on that sizing mode)
        selects the custom-code path here rather than later in synthesis. The
        trial compile is *gated on readiness* because the compiler assumes
        structurally valid DSL: a spec with a residual readiness critical can
        make ``compile_strategy`` raise a non-``CompilerError``, which must not
        abort the loop — that defect is left to the readiness-critique / revise
        path.

        Pre: ``deterministic_ready`` / ``readiness_results`` /
        ``last_readiness_signature`` reflect the latest readiness verdict.
        Post: returns the (possibly repaired) ``spec`` and the refreshed
        ``(readiness_results, last_readiness_signature, deterministic_ready,
        mechanical_repair_count)``. Re-records readiness gates onto
        ``all_gate_results`` and emits ``design_repair`` + records drift only
        when at least one repair action was applied.
        """
        repair_actions: List[RepairAction] = []
        pre_repair_spec: Optional[StrategySpec] = None

        # Stage 1 — mechanical repairs (repair_spec never trial-compiles).
        outcome = repair_spec(spec, config=config)
        if outcome.actions:
            pre_repair_spec = spec.model_copy(deep=True)
            spec = outcome.spec
            repair_actions.extend(outcome.actions)
            # Re-validate only when a repair changed a readiness-relevant
            # field (mechanical repairs always do; the signature catches it).
            repaired_signature = _spec_readiness_signature(spec)
            if repaired_signature != last_readiness_signature:
                readiness_results = self.spec_readiness_gate.validate(
                    spec, phase="design", backtest_config=config
                )
                self.record_gates(readiness_results, all_gate_results, refinement_round=-1)
                last_readiness_signature = repaired_signature
                deterministic_ready = not any(
                    (not r.passed) and r.severity == "critical" for r in readiness_results
                )

        # Stage 2 — trial compile, only on a readiness-clean spec.
        if deterministic_ready:
            compile_action = select_code_path(spec)
            if compile_action is not None:
                if pre_repair_spec is None:
                    pre_repair_spec = spec.model_copy(deep=True)
                spec = spec.model_copy(update={"requires_custom_code": True})
                repair_actions.append(compile_action)
                # Flipping ``requires_custom_code`` can change the
                # readiness verdict (it gates which closed-form gates
                # apply), so re-validate against the flipped spec rather
                # than ride the stale pre-flip verdict.
                repaired_signature = _spec_readiness_signature(spec)
                if repaired_signature != last_readiness_signature:
                    readiness_results = self.spec_readiness_gate.validate(
                        spec, phase="design", backtest_config=config
                    )
                    self.record_gates(readiness_results, all_gate_results, refinement_round=-1)
                    last_readiness_signature = repaired_signature
                    deterministic_ready = not any(
                        (not r.passed) and r.severity == "critical" for r in readiness_results
                    )

        if repair_actions:
            mechanical_repair_count += len(repair_actions)
            emit(
                "design_repair",
                {
                    "round": review_round,
                    "actions": [
                        {
                            "rule": a.rule,
                            "field": a.field,
                            "before": a.before,
                            "after": a.after,
                            "reason": a.reason,
                        }
                        for a in repair_actions
                    ],
                    "now_ready": deterministic_ready,
                },
            )
            if drift_collector is not None:
                drift_collector.record_spec_change(
                    phase="design",
                    agent="MechanicalRepair",
                    before_spec=pre_repair_spec,
                    after_spec=spec,
                    reason="deterministic mechanical auto-repair",
                )

        return (
            spec,
            readiness_results,
            last_readiness_signature,
            deterministic_ready,
            mechanical_repair_count,
        )

    def _review_and_handle_critique(
        self,
        *,
        deterministic_ready: bool,
        spec: StrategySpec,
        rationale: str,
        readiness_results: List[QualityGateResult],
        critique_history: List["SpecCritique"],
        ledger: CritiqueLedger,
        review_round: int,
        mechanical_repair_count: int,
        emit: PhaseCallback,
    ) -> Tuple["SpecCritique", Any, bool]:
        """Produce this round's critique — from the reviewer or readiness.

        Pre: ``critique_history`` / ``ledger`` are the running review state.
        Post: appends one critique to ``critique_history`` and records the round
        on ``ledger`` (returning its delta), then returns
        ``(critique, delta, reviewer_ready)``. On a readiness-clean spec the LLM
        reviewer runs (``started`` → ``completed`` emit) and ``reviewer_ready``
        mirrors ``critique.ready``; otherwise a synthetic readiness critique is
        used (``skipped`` emit) and ``reviewer_ready`` is ``False``. Emits the
        design-review telemetry for the round.
        Raises: ``DesignBudgetExhausted`` (reviewer path only) with the latest
        spec / rationale / repair count attached for the caller's handler.
        """
        if deterministic_ready:
            emit("design_review", {"sub_phase": "started", "round": review_round})
            try:
                critique = self.design_review_agent.run(
                    spec,
                    readiness_results,
                    prior_critiques=critique_history,
                )
            except DesignBudgetExhausted as exc:
                # The budget handler in ``_run_design_loop`` only captures
                # this helper's spec/rationale/counters on the success-return
                # path; surface the latest in-loop spec (post mechanical-
                # repair) and the repair count so the short-circuit record
                # reflects the spec actually evaluated, not the pre-loop
                # draft, and its telemetry still reports the repairs applied.
                exc.latest_spec = spec
                exc.latest_rationale = rationale
                exc.mechanical_repair_count = mechanical_repair_count
                raise
            critique.round = review_round
            critique_history.append(critique)
            delta = ledger.record_round(critique)
            emit(
                "design_review",
                {
                    "sub_phase": "completed",
                    "round": review_round,
                    "ready": critique.ready,
                    "issue_count": len(critique.issues),
                    "regressed_count": len(delta.regressed),
                },
            )
            _emit_design_review_telemetry(emit, review_round, ledger, delta)
            return critique, delta, critique.ready

        # Synthesise a critique from the readiness findings so ``revise()``
        # sees the same shape regardless of which path produced the verdict.
        # The reviewer is intentionally skipped this round.
        critique = _critique_from_readiness(readiness_results)
        critique.round = review_round
        critique_history.append(critique)
        delta = ledger.record_round(critique)
        emit(
            "design_review",
            {
                "sub_phase": "skipped",
                "round": review_round,
                "reason": "readiness_critical",
                "regressed_count": len(delta.regressed),
            },
        )
        _emit_design_review_telemetry(emit, review_round, ledger, delta)
        return critique, delta, False

    def _revise_with_regression_notice(
        self,
        *,
        spec: StrategySpec,
        rationale: str,
        critique: "SpecCritique",
        delta: Any,
        critique_history: List["SpecCritique"],
        strategy_id: str,
        mechanical_repair_count: int,
        drift_collector: Optional[_DriftCollector],
    ) -> Tuple[StrategySpec, str]:
        """Revise the spec from the round's critique, flagging regressions.

        Pre: this round did not stop the loop (not ready / stalled / capped).
        Post: returns ``(spec, rationale)`` — the revised candidate built from
        the designer's payload and its new rationale. Any regression (an issue
        resolved earlier that reappeared) is fed back to the designer as an
        explicit "do not reintroduce" notice (advisory, not a hard block — a
        hard block risks deadlock if the model cannot avoid it). Records the
        spec drift when a collector is present.
        Raises: ``DesignBudgetExhausted`` with the latest spec / rationale /
        repair count attached — revise has not yet produced a new spec, so the
        latest fully-realised spec is the current (post mechanical-repair) one.
        """
        regression_notice = _format_regression_notice(critique, delta.regressed)
        prev_spec = spec.model_copy(deep=True)
        try:
            strategy_dict, rationale = self.design_agent.revise(
                spec,
                critique,
                prior_critiques=critique_history,
                regression_notice=regression_notice,
            )
        except DesignBudgetExhausted as exc:
            exc.latest_spec = spec
            exc.latest_rationale = rationale
            exc.mechanical_repair_count = mechanical_repair_count
            raise
        spec = self._build_spec_from_dict(strategy_dict, strategy_id=strategy_id)
        if drift_collector is not None:
            drift_collector.record_spec_change(
                phase="design_review",
                agent="DesignAgent",
                before_spec=prev_spec,
                after_spec=spec,
                reason=critique.rationale if hasattr(critique, "rationale") else str(critique),
            )
        return spec, rationale

    def _build_spec_from_dict(
        self, strategy_dict: Dict[str, Any], *, strategy_id: str
    ) -> StrategySpec:
        """Thin instance wrapper over :func:`build_spec_from_dict`.

        Retained so the orchestrator's existing call sites keep their method
        form; the construction logic lives in the module-level function so it
        can be unit-tested without instantiating the orchestrator.
        """
        return build_spec_from_dict(strategy_dict, strategy_id=strategy_id)

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
        phase_back_count: int = 0,
        drift_collector: Optional[_DriftCollector] = None,
        design_context: Optional[_DesignPersistContext] = None,
    ) -> Optional[StrategyLabRecord]:
        """Run spec validation before the refinement loop.

        Pre: ``spec`` is a constructed ``StrategySpec`` that has already
        passed the design-phase ``SpecReadinessGate`` check (the design
        loop is the sole caller of that gate with ``phase="design"``);
        ``all_gate_results`` is the orchestrator's running gate list that
        the caller persists.
        Post: returns a short-circuit ``StrategyLabRecord`` when a critical
        gate fires (and ``all_gate_results`` is extended in place with the
        pre-synthesis gates); returns ``None`` to signal the caller can
        continue into the synthesis refinement loop.

        The "strategy_code is missing" critical from StrategySpecValidator
        is deliberately filtered: post-design we always have *some* code
        (the loop's existing safety + regeneration paths repair degenerate
        inputs), so short-circuiting on that critical would regress a
        recoverable case into an outright failure.
        """
        # ``config`` is intentionally not consulted by the readiness gate
        # here — the design loop owns the ``phase="design"`` readiness
        # check, and the round-0 readiness call inside ``_run_synthesis_loop``
        # carries ``phase="synthesis"``. The argument is still threaded
        # into the short-circuit record below so persistence sees the
        # same config the design phase saw.
        pre_spec_gates_raw = self.strategy_validator.validate(spec)
        pre_spec_gates = [
            g
            for g in pre_spec_gates_raw
            if not (g.severity == "critical" and g.details.startswith("strategy_code is missing"))
        ]
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
            design_context=design_context,
            phase_back_count=phase_back_count,
            drift_collector=drift_collector,
        )

    def _cached_run_strategy_code(
        self,
        code: str,
        market_data: Dict[str, List[OHLCVBar]],
        config: BacktestConfig,
        *,
        strategy: StrategySpec,
    ) -> StrategyRunResult:
        """Run ``code`` through the attempt-scoped :class:`BacktestCache`.

        Routes the module-level ``run_strategy_code`` (so test monkeypatches
        of ``orchestrator.run_strategy_code`` still apply) and memoizes on
        ``(code, market_data, config)``. The cache is created lazily so a
        sub-loop invoked directly in a test — outside ``_run_design_attempt``
        — still works (with a degenerate one-entry cache).

        Pre: ``code`` is non-empty; ``market_data`` is the hoisted per-symbol
        OHLCV dict for the attempt.
        Post: returns the ``StrategyRunResult`` for ``code`` — a fresh run on
        the first call with a given key, the stored result on subsequent ones.
        """
        cache = getattr(self, "_backtest_cache", None)
        if cache is None:
            cache = self._backtest_cache = BacktestCache()
        result, _hit = cache.get_or_run(
            code, market_data, config, strategy=strategy, runner=run_strategy_code
        )
        return result

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
        drift_collector: Optional[_DriftCollector] = None,
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
        open_position_entry_reasons: List[str] = []
        metrics = compute_metrics([], config.initial_capital, config.start_date, config.end_date)
        execution_succeeded = False
        market_data: Optional[Dict[str, List[OHLCVBar]]] = None
        requested_symbols: List[str] = []
        fetched_symbols: List[str] = []
        provider_used: Dict[str, str] = {}
        max_rounds_exhausted = False
        # Tracks whether the LAST executed round (including any
        # ``_handle_critical_anomalies`` recovery) surfaced the harness's
        # runtime ``lookahead_violation`` (``error_type == LOOKAHEAD``).
        # Threaded onto the synthesis outcome so the verification phase
        # can stamp the cause onto ``acceptance_reason`` instead of the
        # generic ``publication_disabled`` message.
        runtime_lookahead_violation = False
        predicate_conformance_attempts = 0
        # Captured at trade-collection time for the round whose backtest is
        # persisted: True when that round ran custom code whose
        # predicate-conformance check was demoted (warning) past the retry
        # budget. A later round that passes conformance but fails before
        # collecting trades does not clear an earlier demoted round's value.
        ran_on_non_conforming_code = False

        for round_num in range(MAX_CODE_REFINEMENT_ROUNDS):
            round_gate_results: List[QualityGateResult] = []

            # Deterministic post-synthesis normalization: guarantee the
            # ``UNIVERSE`` constant and the ``on_bar`` symbol guard are present
            # and conformant before any gate sees the code, so the conformance
            # symbol gate never burns a refinement round on boilerplate that is
            # fully determined by ``spec.target_symbols``. Idempotent (strips
            # then reinserts), so it is a no-op on already-conformant code and
            # safe to apply to both the initial and every refined code variant.
            before_inject = code
            code = inject_universe_and_guard(code, spec)
            if code != before_inject:
                # Keep ``spec.strategy_code`` in lockstep with the code that is
                # actually executed/gated this round (refinement maintains the
                # same invariant via ``_apply_updates``). Downstream consumers
                # such as ``_maybe_attach_coverage_report`` re-run probes off
                # ``spec.strategy_code`` and would otherwise analyse stale,
                # pre-injection source.
                spec.strategy_code = code
                if drift_collector is not None:
                    drift_collector.record_code_change(
                        phase="synthesis",
                        agent="universe_injector",
                        before_code=before_inject,
                        after_code=code,
                        reason="deterministic UNIVERSE + symbol-guard injection",
                    )

            # ── 2a: VALIDATE (code safety + spec readiness on round 0) ───
            round_gate_results, predicate_conformance_attempts = (
                self._run_synthesis_validation_gates(
                    spec=spec,
                    code=code,
                    config=config,
                    round_num=round_num,
                    predicate_conformance_attempts=predicate_conformance_attempts,
                    all_gate_results=all_gate_results,
                    emit=emit,
                )
            )

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
                    f"- [{g.gate_name}{(':' + g.rule_id) if g.rule_id else ''}] {g.details}"
                    for g in critical_failures
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
                    drift_collector=drift_collector,
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
                fetch = self._fetch_market_data_for_synthesis(
                    spec=spec,
                    config=config,
                    round_num=round_num,
                    all_gate_results=all_gate_results,
                    emit=emit,
                )
                requested_symbols = fetch.requested_symbols
                fetched_symbols = fetch.fetched_symbols
                provider_used = fetch.provider_used
                market_data = fetch.data
                if fetch.should_break:
                    break

            # ── 2c: EXECUTE (syntax / runtime correctness) ───────────
            emit("backtesting", {"sub_phase": "running_code", "refinement_round": round_num})
            exec_result = self._cached_run_strategy_code(code, market_data, config, strategy=spec)
            runtime_lookahead_violation = exec_result.error_type == "lookahead_violation"

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
                    f"Error type: {exec_result.error_type}\nstderr:\n{exec_result.stderr}"
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
                    drift_collector=drift_collector,
                )
                if exhausted:
                    max_rounds_exhausted = True
                    break
                continue

            # ── 2d: COLLECT TRADES + target-symbol coverage on trades ─
            trades = exec_result.trades
            # This round's executed code is what produced the persisted trades;
            # attribute the conformance verdict to it (overwriting any earlier
            # round's value) so the flag tracks the backtest that survives.
            ran_on_non_conforming_code = _round_demoted_conformance(round_gate_results)
            open_position_entry_reasons = getattr(exec_result, "open_position_entry_reasons", [])

            trade_coverage_gates = self.target_symbol_coverage_gate.check_trades(spec, trades)
            self.record_gates(trade_coverage_gates, all_gate_results, refinement_round=round_num)
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
            evaluation = self._evaluate_synthesis_round(
                spec=spec,
                code=code,
                trades=trades,
                exec_result=exec_result,
                market_data=market_data,
                config=config,
                round_num=round_num,
                ran_on_non_conforming_code=ran_on_non_conforming_code,
                all_gate_results=all_gate_results,
                refinement_attempts=refinement_attempts,
                zero_trade_attempts=zero_trade_attempts,
                emit=emit,
                drift_collector=drift_collector,
            )
            spec, code = evaluation.spec, evaluation.code
            trades, metrics = evaluation.trades, evaluation.metrics
            ran_on_non_conforming_code = evaluation.ran_on_non_conforming_code
            exec_result = evaluation.exec_result
            runtime_lookahead_violation = evaluation.runtime_lookahead_violation
            if evaluation.action == "exhausted":
                max_rounds_exhausted = True
                break
            if evaluation.action == "continue":
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
            provider_used=provider_used,
            open_position_entry_reasons=open_position_entry_reasons,
            runtime_lookahead_violation=runtime_lookahead_violation,
            ran_on_non_conforming_code=ran_on_non_conforming_code,
        )

    def _run_synthesis_validation_gates(
        self,
        *,
        spec: StrategySpec,
        code: str,
        config: BacktestConfig,
        round_num: int,
        predicate_conformance_attempts: int,
        all_gate_results: List[QualityGateResult],
        emit: PhaseCallback,
    ) -> Tuple[List[QualityGateResult], int]:
        """Run one round's validation gates and record them.

        Pre: ``code`` has had the deterministic universe/guard injection
        applied; ``all_gate_results`` is the running gate list.
        Post: returns ``(round_gate_results, predicate_conformance_attempts)``.
        ``round_gate_results`` holds this round's gate results — spec readiness
        (round 0 only), code safety, code conformance, and predicate
        conformance — and is recorded onto ``all_gate_results`` in place via
        ``record_gates``. Predicate conformance runs (and extends
        ``round_gate_results``) only when no prior validation gate fired a
        critical, preserving the gate-execution ordering exactly; its attempt
        counter is incremented and returned when it fires a critical.
        """
        emit("coding", {"sub_phase": "started", "refinement_round": round_num})
        round_gate_results: List[QualityGateResult] = []
        if round_num == 0:
            round_gate_results.extend(
                self.spec_readiness_gate.validate(spec, phase="synthesis", backtest_config=config)
            )
        code_gates = self.code_safety_checker.check(code, spec)
        round_gate_results.extend(code_gates)
        conformance_gates = self.code_conformance_gate.check(code, spec)
        round_gate_results.extend(conformance_gates)
        # Predicate conformance only runs when every prior validation gate
        # (spec readiness, code safety, code conformance) is clean. Checking
        # code that an earlier gate already flagged as critical adds noisy
        # rule_id criticals on top of the cleaner upstream critical.
        if not any(not g.passed and g.severity == "critical" for g in round_gate_results):
            pred_conf_gates = self.predicate_conformance_gate.check(
                code,
                spec,
                attempt=predicate_conformance_attempts,
            )
            round_gate_results.extend(pred_conf_gates)
            if any(not g.passed and g.severity == "critical" for g in pred_conf_gates):
                predicate_conformance_attempts += 1
        self.record_gates(round_gate_results, all_gate_results, refinement_round=round_num)
        return round_gate_results, predicate_conformance_attempts

    def _fetch_market_data_for_synthesis(
        self,
        *,
        spec: StrategySpec,
        config: BacktestConfig,
        round_num: int,
        all_gate_results: List[QualityGateResult],
        emit: PhaseCallback,
    ) -> _SynthesisFetchResult:
        """Fetch market data once for the synthesis loop.

        Pre: only called when ``market_data`` has not yet been fetched.
        Post: returns a ``_SynthesisFetchResult`` carrying the OHLCV payload and
        the symbol/provider audit trail. ``should_break=True`` when no data came
        back (records the ``market_data`` gate) or a critical fetch-coverage
        failure fired (records the coverage gates) — the caller adopts the
        symbol/provider fields regardless and breaks the loop when set. Records
        the relevant gates onto ``all_gate_results`` in place.
        """
        emit("backtesting", {"sub_phase": "fetching_data"})
        fetch = self._fetch_market_data(spec, config)
        requested_symbols = list(fetch.requested_symbols)
        fetched_symbols = list(fetch.fetched_symbols)
        provider_used = dict(fetch.provider_used)
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
            return _SynthesisFetchResult(
                data=market_data,
                requested_symbols=requested_symbols,
                fetched_symbols=fetched_symbols,
                provider_used=provider_used,
                should_break=True,
            )
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
        self.record_gates(fetch_coverage_gates, all_gate_results, refinement_round=round_num)
        should_break = any(not g.passed and g.severity == "critical" for g in fetch_coverage_gates)
        return _SynthesisFetchResult(
            data=market_data,
            requested_symbols=requested_symbols,
            fetched_symbols=fetched_symbols,
            provider_used=provider_used,
            should_break=should_break,
        )

    def _evaluate_synthesis_round(
        self,
        *,
        spec: StrategySpec,
        code: str,
        trades: List[TradeRecord],
        exec_result: StrategyRunResult,
        market_data: Dict[str, List[OHLCVBar]],
        config: BacktestConfig,
        round_num: int,
        ran_on_non_conforming_code: bool,
        all_gate_results: List[QualityGateResult],
        refinement_attempts: List[str],
        zero_trade_attempts: List[str],
        emit: PhaseCallback,
        drift_collector: Optional[_DriftCollector],
    ) -> _SynthesisEvaluateResult:
        """Compute metrics, run the anomaly gates, and route any recovery.

        Pre: this round executed cleanly and collected ``trades`` through the
        coverage gate; ``ran_on_non_conforming_code`` is the verdict captured at
        trade collection.
        Post: returns a ``_SynthesisEvaluateResult``. ``action="success"`` when
        no critical anomaly fired (the caller marks ``execution_succeeded``);
        otherwise ``_handle_critical_anomalies`` runs and the result carries the
        recovered ``spec``/``code``/``trades``/``metrics``/``exec_result`` with
        ``action="continue"`` (retry next round) or ``"exhausted"`` (budget
        spent). ``ran_on_non_conforming_code`` is replaced only when a committed
        repair supplied a fresh verdict (non-``None``). Records the anomaly
        gates onto ``all_gate_results`` in place.
        """
        metrics = compute_metrics(
            trades, config.initial_capital, config.start_date, config.end_date
        )
        # ``compute_metrics`` builds from the trade ledger alone; carry the
        # engine's exit-rule firing counters from this run onto ``metrics``
        # so the downstream ``ExitRuleConformanceGate`` can reconcile
        # engine-attributed closes against recorded firings.
        _attach_execution_diagnostics(metrics=metrics, exec_result=exec_result)

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
            market_data=market_data,
        )
        self.record_gates(anomaly_gates, all_gate_results, refinement_round=round_num)

        critical_anomalies = [g for g in anomaly_gates if not g.passed and g.severity == "critical"]
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
                drift_collector=drift_collector,
            )
            spec, code = recovery.spec, recovery.code
            trades, metrics = recovery.trades, recovery.metrics
            # A committed zero-trade repair replaced the persisted trades
            # with new code; adopt its conformance verdict. The generic
            # refine path leaves the trades (and so the verdict) unchanged
            # and signals that with ``None``.
            if recovery.ran_on_non_conforming_code is not None:
                ran_on_non_conforming_code = recovery.ran_on_non_conforming_code
            exec_result = recovery.exec_result
            # Even if the code is technically correct, an exhausted cycle
            # leaves ``action="exhausted"`` so the caller keeps
            # ``execution_succeeded=False`` and ``is_winning`` stays False —
            # paper-trading must not fire on a "failed: max_refinement_rounds"
            # record.
            return _SynthesisEvaluateResult(
                action="exhausted" if recovery.exhausted else "continue",
                spec=spec,
                code=code,
                trades=trades,
                metrics=metrics,
                exec_result=exec_result,
                ran_on_non_conforming_code=ran_on_non_conforming_code,
                runtime_lookahead_violation=exec_result.error_type == "lookahead_violation",
            )

        return _SynthesisEvaluateResult(
            action="success",
            spec=spec,
            code=code,
            trades=trades,
            metrics=metrics,
            exec_result=exec_result,
            ran_on_non_conforming_code=ran_on_non_conforming_code,
            runtime_lookahead_violation=exec_result.error_type == "lookahead_violation",
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
        drift_collector: Optional[_DriftCollector] = None,
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
          1. If diagnostics classify the failure as ``ENTRY_WITH_NO_EXIT``
             (entries filled but engine-owned exits never fired), raise
             ``SpecImplementabilityError`` to route the cycle back to
             redesign / spec refinement. This category has no valid
             code-level repair — exits are engine-owned, a manual close is
             rejected by the conformance gate, and ``exit_rules`` spec edits
             are dropped by the ``risk_limits``-only repair whitelist — so
             only the designer (which can rewrite ``exit_rules``) can fix it.
          2. Else if diagnostics carry a ``zero_trade_category`` AND there is
             market data, ask the specialised repair agent first. A
             committed proposal has already cleared safety + fresh
             backtest + anomaly gates, so we use it directly.
          3. Otherwise (or if the repair did not commit), fall through
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

        # ── 2: Route a non-firing engine-owned exit to redesign / spec refinement ──
        # ``ENTRY_WITH_NO_EXIT`` (entries filled, exits never fired, positions
        # still open at window end) has no actionable code-level repair: exits
        # are engine-owned, a manual code close is rejected by the conformance
        # gate, and ``exit_rules`` spec edits are dropped by the repair
        # whitelist (``risk_limits`` only). The only real lever is the spec's
        # exit rules, which only the designer can revise — so phase back to
        # design instead of burning refinement rounds on the code-only repair
        # loop, where the agent now correctly proposes nothing.
        diag = exec_result.execution_diagnostics
        if diag is not None and diag.zero_trade_category == "ENTRY_WITH_NO_EXIT":
            emit(
                "coding",
                {
                    "sub_phase": "routed_to_redesign",
                    "refinement_round": round_num,
                    "via": "entry_with_no_exit",
                },
            )
            raise SpecImplementabilityError(
                (
                    "ENTRY_WITH_NO_EXIT: entries filled but engine-owned exits "
                    "never fired in the test window; positions remained open at "
                    "the end. No code-level repair is possible (exits are "
                    "engine-owned and exit_rules spec edits are not honoured by "
                    "the code-repair loop). Revise spec.exit_rules — loosen or "
                    "retune the stop-loss / take-profit / signal-exit rules so "
                    f"exits can fire. Diagnostics:\n{failure_details}"
                ),
                failure_phase="evaluation",
                last_spec=spec,
                last_code=code,
                drift_collector=drift_collector,
            )

        # ── 3: Specialised zero-trade repair (if diagnostics support it) ──
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
                assert zt_outcome.new_exec_result is not None, (
                    "committed ZTR must carry new_exec_result"
                )
                refinement_attempts.append(
                    f"zero-trade repair: {zt_outcome.changes_made}"
                    if zt_outcome.changes_made
                    else "zero-trade repair"
                )
                if drift_collector is not None:
                    zt_reason = zt_outcome.changes_made or "zero-trade repair"
                    drift_collector.record_spec_change(
                        phase="verification",
                        agent="ZeroTradeRepairer",
                        before_spec=spec,
                        after_spec=zt_outcome.new_spec,
                        reason=zt_reason,
                    )
                    drift_collector.record_code_change(
                        phase="verification",
                        agent="ZeroTradeRepairer",
                        before_code=code,
                        after_code=zt_outcome.new_code,
                        reason=zt_reason,
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
                # The committed repair replaces the persisted trades/code;
                # re-check predicate conformance on it so the non-conforming flag
                # describes the repaired backtest (the repairer does not re-run
                # the gate). The caller adopts this value only on commit; the
                # generic-refine path below leaves it ``None`` (trades unchanged).
                ztr_non_conforming = self._committed_code_conformance_verdict(
                    zt_outcome.new_code,
                    zt_outcome.new_spec,
                    all_gate_results=all_gate_results,
                    refinement_round=round_num,
                    gate_name_prefix="zero_trade_repair_",
                )
                return _AnomalyRecoveryOutcome(
                    spec=zt_outcome.new_spec,
                    code=zt_outcome.new_code,
                    trades=zt_outcome.new_trades,
                    metrics=zt_outcome.new_metrics,
                    exec_result=zt_outcome.new_exec_result,
                    exhausted=False,
                    ran_on_non_conforming_code=ztr_non_conforming,
                )

        # ── 4: Generic refinement (or exhaust the round budget) ──
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
            drift_collector=drift_collector,
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
        assert align_round >= 0, "align_round must be non-negative"
        assert isinstance(market_data, dict) and market_data, "market_data must be non-empty"

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
        report, gate_results = self._run_alignment_audit(
            spec=spec,
            code=code,
            trades=trades,
            metrics=metrics,
            prior_attempts=alignment_attempts,
            market_data=market_data,
            config=config,
        )
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
                        "description": i.description[:160],
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
                        "details": f.details[:160],
                    }
                    for f in report.alignment_findings[:10]
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
                    "details": "; ".join(g.details for g in critical_safety)[:400],
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
        new_trades = align_exec.trades
        new_metrics = compute_metrics(
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

        anomaly_gates = self.anomaly_detector.check(
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
        realism_results = self._run_realism_gates(
            spec=spec,
            trades=trades,
            metrics=metrics,
            config=config,
            market_data=market_data,
            execution_succeeded=execution_succeeded,
            open_position_entry_reasons=open_position_entry_reasons,
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

        return _VerificationOutcome(
            metrics=metrics,
            is_winning=is_winning,
            upstream_admitted=upstream_admitted,
            acceptance_results=acceptance_results,
            walk_forward_failed=walk_forward_failed,
            exit_rule_conformance_passed=exit_rule_conformance_passed,
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
                    fallback_reasons.append("; ".join(g.details for g in fallback_criticals))
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
            detail = "; ".join(r.details for r in conformance_criticals)
            suffix = (
                f"exit_rule_conformance_failed: {detail}"
                if detail
                else "exit_rule_conformance_failed: engine enforcement leaked"
            )
            metrics, upstream_admitted = _apply_veto_to_acceptance_reason(
                metrics, suffix, upstream_admitted=upstream_admitted
            )

        if execution_succeeded and trades and not realism_passed:
            detail = "; ".join(r.details for r in realism_critical)
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
    ) -> List[QualityGateResult]:
        """Run verification-phase realism gates and return their results.

        Preconditions:
          - Called from :meth:`_run_verification_phase` between walk-forward
            evaluation and the publication-veto block.
          - ``metrics`` carries the post-walk-forward backtest result.
          - ``config`` is the run's :class:`BacktestConfig`.
          - ``market_data`` is the per-symbol bar table used for the run;
            the liquidity gate self-skips when this is ``None``.
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
            (``api.main._strategy_lab_worker`` force-enables the flag).
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
        provider_used: Dict[str, str],
        max_rounds_exhausted: bool,
        execution_succeeded: bool,
        is_winning: bool,
        trades_aligned: bool,
        refinement_rounds: int,
        alignment_rounds: int,
        all_gate_results: List[QualityGateResult],
        emit: PhaseCallback,
        ran_on_non_conforming_code: bool = False,
        design_context: Optional[_DesignPersistContext] = None,
        alignment_findings: Optional[List[AlignmentFinding]] = None,
        phase_back_count: int = 0,
        drift_collector: Optional[_DriftCollector] = None,
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
        # Issue #533 — structured provenance block.
        # ``target_symbols`` is the spec's explicit request (or [] when the
        # spec relied on the asset-class fallback universe); it is *distinct*
        # from ``requested_symbols`` (the resolved universe handed to the
        # fetcher). ``as_of`` mirrors ``audit.data_snapshot_id`` so a re-run
        # of the saved record replays the same snapshot. ``legacy_fingerprint``
        # exposes the existing ``BacktestResult.dataset_fingerprint`` at the
        # record level so the CLI doesn't need to dig into ``metrics``.
        as_of = (getattr(spec, "audit", None) and spec.audit.data_snapshot_id) or None
        data_provenance = DataProvenance(
            target_symbols=list(spec.target_symbols or []),
            fetched_symbols=list(fetched_symbols),
            traded_symbols=sorted({t.symbol for t in trades}),
            provider_used=dict(provider_used),
            as_of=as_of,
            legacy_fingerprint=metrics.dataset_fingerprint,
        )
        # Materialise the per-rule findings once — consumed both by the
        # backtest record and the rule-implementation map below.
        normalized_alignment_findings = list(alignment_findings or [])
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
            data_provenance=data_provenance,
            alignment_findings=normalized_alignment_findings,
        )

        design_context = design_context or _DesignPersistContext()
        dc = drift_collector or _DriftCollector()
        gate_timeline = dc.gate_timeline + [
            GateEvent(
                phase=g.phase,
                gate_name=g.gate_name,
                passed=g.passed,
                severity=g.severity,
                details=g.details,
                timestamp=g.evaluated_at,
            )
            for g in all_gate_results
        ]
        rule_impl_map = _build_rule_implementation_map(spec, normalized_alignment_findings, code)
        lab_record_id = f"lab-{uuid.uuid4().hex[:8]}"
        loop_telemetry = _finalize_loop_telemetry(
            design_context,
            all_gate_results,
            spec,
            code,
            ran_on_non_conforming_code=ran_on_non_conforming_code,
        )
        record = StrategyLabRecord(
            lab_record_id=lab_record_id,
            strategy=spec,
            backtest=backtest_record,
            is_winning=is_winning,
            strategy_rationale=rationale,
            analysis_narrative=narrative,
            created_at=now_iso,
            refinement_rounds=refinement_rounds,
            design_rounds=design_context.rounds,
            critiques=[c.model_dump() for c in design_context.critiques],
            quality_gate_results=[g.model_dump() for g in all_gate_results],
            strategy_code=code,
            original_spec=original_spec,
            original_code=original_code,
            spec_implementability_phase_backs=phase_back_count,
            spec_history=list(dc.spec_history),
            code_history=list(dc.code_history),
            gate_timeline=gate_timeline,
            rule_implementation_map=rule_impl_map,
            loop_telemetry=loop_telemetry,
            ran_on_non_conforming_code=ran_on_non_conforming_code,
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
                "phase_back_count": phase_back_count,
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
        design_attempt: int = 0,
        phase_back_count: int = 0,
        drift_collector: Optional[_DriftCollector] = None,
        cumulative_gate_results: Optional[List[QualityGateResult]] = None,
    ) -> StrategyLabRecord:
        """One design + refinement attempt. May raise
        ``SpecImplementabilityError`` to signal a need to re-enter the
        design phase; the outer ``run_cycle`` catches and re-routes.

        The design phase runs a bounded loop of (DesignAgent → readiness
        gate → DesignReviewAgent → DesignAgent.revise) until either the
        reviewer marks the spec ready or the round budget is exhausted.
        Exhaustion short-circuits the cycle without ever running code.

        ``phase_back_count`` is the number of prior phase-backs in the
        enclosing ``run_cycle`` and is stamped onto the persisted record
        so the breakdown (success after N phase-backs, short-circuit
        after N phase-backs) is observable post hoc.
        """
        # Reset per-attempt counters so a re-entry starts fresh.
        self._consecutive_spec_mutation_rounds = {}
        # Fresh, attempt-scoped backtest memo. Discarding it per attempt keeps
        # a cached result from ever crossing a market-data snapshot: the same
        # code re-run against the same hoisted ``market_data`` + ``config``
        # (alignment re-checks, determinism re-checks, audit re-backtests)
        # short-circuits to the stored ``StrategyRunResult``.
        self._backtest_cache = BacktestCache()

        all_gate_results: List[QualityGateResult] = (
            cumulative_gate_results if cumulative_gate_results is not None else []
        )
        refinement_attempts: List[str] = []
        zero_trade_attempts: List[str] = []
        if drift_collector is None:
            drift_collector = _DriftCollector()

        # ── Phase 1: DESIGN + REVIEW LOOP ──────────────────────────────
        design_phase = self._orchestrate_design_and_review(
            prior_records=prior_records,
            signal_brief=signal_brief,
            directives=directives,
            exclude_asset_classes=exclude_asset_classes,
            config=config,
            all_gate_results=all_gate_results,
            emit=emit,
            design_attempt=design_attempt,
            phase_back_count=phase_back_count,
            drift_collector=drift_collector,
        )
        if design_phase.record is not None:
            return design_phase.record
        spec = design_phase.spec
        rationale = design_phase.rationale
        design_context = design_phase.design_context

        # ── Phase 1b: CODE SYNTHESIS ──────────────────────────────────
        code_synthesis = self._synthesize_initial_code(
            spec=spec,
            config=config,
            rationale=rationale,
            all_gate_results=all_gate_results,
            design_attempt=design_attempt,
            phase_back_count=phase_back_count,
            drift_collector=drift_collector,
            design_context=design_context,
            emit=emit,
        )
        if code_synthesis.record is not None:
            return code_synthesis.record
        code = code_synthesis.code
        original_spec = code_synthesis.original_spec
        original_code = code_synthesis.original_code
        config = code_synthesis.config

        # ── Phases 1b–2.5: PRE-SYNTHESIS GATE → REFINEMENT → ALIGNMENT ─
        refine_align = self._orchestrate_refinement_and_alignment(
            spec=spec,
            code=code,
            config=config,
            original_spec=original_spec,
            original_code=original_code,
            rationale=rationale,
            all_gate_results=all_gate_results,
            refinement_attempts=refinement_attempts,
            zero_trade_attempts=zero_trade_attempts,
            emit=emit,
            design_attempt=design_attempt,
            phase_back_count=phase_back_count,
            drift_collector=drift_collector,
            design_context=design_context,
        )
        if refine_align.record is not None:
            return refine_align.record
        synthesis = refine_align.synthesis
        alignment_outcome = refine_align.alignment
        spec = alignment_outcome.spec
        code = alignment_outcome.code
        trades = alignment_outcome.trades
        metrics = alignment_outcome.metrics
        market_data = synthesis.market_data
        requested_symbols = synthesis.requested_symbols
        fetched_symbols = synthesis.fetched_symbols
        provider_used = synthesis.provider_used
        execution_succeeded = synthesis.execution_succeeded
        max_rounds_exhausted = synthesis.max_rounds_exhausted
        open_position_entry_reasons = synthesis.open_position_entry_reasons
        ran_on_non_conforming_code = alignment_outcome.ran_on_non_conforming_code
        alignment_rounds = alignment_outcome.alignment_rounds
        trades_aligned = alignment_outcome.trades_aligned
        alignment_reports = alignment_outcome.alignment_reports

        # ── Phases 2.6–3: TRIAL COUNTING → VERIFICATION → ANALYSIS ─────
        metrics, is_winning, narrative = self._orchestrate_verification_and_analysis(
            spec=spec,
            trades=trades,
            metrics=metrics,
            market_data=market_data,
            config=config,
            execution_succeeded=execution_succeeded,
            trades_aligned=trades_aligned,
            alignment_reports=alignment_reports,
            all_gate_results=all_gate_results,
            runtime_lookahead_violation=synthesis.runtime_lookahead_violation,
            open_position_entry_reasons=open_position_entry_reasons,
            refinement_attempts=refinement_attempts,
            rationale=rationale,
            emit=emit,
        )

        # ═══ Phase 4 → exit: BACKTEST_AND_VERIFICATION → ∅ ════════════
        # Terminal transition out of the last named phase. ``to_phase``
        # is ``None``; ``spec_hash``/``code_hash`` must match the values
        # emitted on the previous two boundaries within this design
        # attempt — the integration test in
        # ``test_strategy_lab_phase_transitions.py`` asserts this.
        _emit_phase_transition(
            emit,
            from_phase=Phase.BACKTEST_AND_VERIFICATION,
            to_phase=None,
            spec=spec,
            code=code,
            attempt=design_attempt,
        )

        # ── Phase 4: RECORD ───────────────────────────────────────────
        return self._extract_findings_and_assemble_record(
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
            provider_used=provider_used,
            max_rounds_exhausted=max_rounds_exhausted,
            execution_succeeded=execution_succeeded,
            is_winning=is_winning,
            trades_aligned=trades_aligned,
            refinement_attempts=refinement_attempts,
            alignment_rounds=alignment_rounds,
            all_gate_results=all_gate_results,
            ran_on_non_conforming_code=ran_on_non_conforming_code,
            design_context=design_context,
            alignment_reports=alignment_reports,
            phase_back_count=phase_back_count,
            drift_collector=drift_collector,
            emit=emit,
        )

    def _orchestrate_design_and_review(
        self,
        *,
        prior_records: List[StrategyLabRecord],
        signal_brief: Optional[SignalIntelligenceBriefV1],
        directives: List[str],
        exclude_asset_classes: Optional[List[str]],
        config: BacktestConfig,
        all_gate_results: List[QualityGateResult],
        emit: PhaseCallback,
        design_attempt: int,
        phase_back_count: int,
        drift_collector: _DriftCollector,
    ) -> _DesignPhaseResult:
        """Run the bounded design + review loop and gate entry to synthesis.

        Pre: ``all_gate_results`` is the running gate list for this attempt.
        Post: returns a ``_DesignPhaseResult``. When the loop did not reach
        readiness (round-cap / stall / LLM-budget), ``record`` carries the
        short-circuit ``StrategyLabRecord`` and the caller returns it. On
        readiness, ``record`` is ``None``, the DESIGN_REVIEW → CODE_SYNTHESIS
        boundary event is emitted, and the converged ``spec`` / ``rationale`` /
        ``design_context`` are returned for code synthesis.
        """
        design_outcome = self._run_design_loop(
            prior_records=prior_records,
            signal_brief=signal_brief,
            directives=directives,
            exclude_asset_classes=exclude_asset_classes,
            config=config,
            all_gate_results=all_gate_results,
            emit=emit,
            design_attempt=design_attempt,
            drift_collector=drift_collector,
        )
        spec = design_outcome.spec
        rationale = design_outcome.rationale
        design_context = _DesignPersistContext(
            rounds=design_outcome.rounds,
            critiques=list(design_outcome.critique_history),
            stop_reason=design_outcome.stop_reason,
            loop_telemetry=dict(design_outcome.loop_telemetry),
        )

        if not design_outcome.ready:
            last_rationale = (
                design_outcome.critique_history[-1].rationale
                if design_outcome.critique_history
                else "(none)"
            )
            # A not-ready outcome has two causes; pick the status the
            # operator needs to see. Budget exhaustion is the cost kill
            # switch — surface it distinctly from genuine non-convergence.
            if design_outcome.budget_exhausted:
                short_circuit_status = "failed: budget_exhausted"
                _budget = active_budget()
                calls_made = _budget.calls_made if _budget is not None else 0
                limit = _budget.limit if _budget is not None else 0
                abort_reason = (
                    f"Design phase exhausted its LLM-call budget "
                    f"({calls_made}/{limit} calls) after {design_context.rounds} "
                    f"round(s); last critique: {last_rationale}"
                )
            elif design_outcome.stop_reason == "stalled":
                # Open-issue set stopped shrinking — the loop oscillated rather
                # than converged. Surface distinctly from honest round-cap
                # exhaustion so operators and audits can tell them apart.
                short_circuit_status = "failed: design_stalled"
                abort_reason = (
                    f"Design loop stalled — the open-issue set was unchanged for "
                    f"{_design_review_stall_rounds()} consecutive round(s) after "
                    f"{design_context.rounds} round(s); last critique: {last_rationale}"
                )
            else:
                short_circuit_status = "failed: design_not_ready"
                abort_reason = (
                    f"Design did not reach readiness after {design_context.rounds} "
                    f"round(s); last critique: {last_rationale}"
                )
            emit("designing", {"sub_phase": "aborted", "reason": abort_reason})
            return _DesignPhaseResult(
                record=self._build_short_circuit_record(
                    spec=spec,
                    config=config,
                    code="",
                    original_spec=spec,
                    original_code="",
                    rationale=rationale,
                    all_gate_results=all_gate_results,
                    refinement_attempts=[],
                    short_circuit_status=short_circuit_status,
                    short_circuit_reason=abort_reason,
                    emit=emit,
                    design_context=design_context,
                    phase_back_count=phase_back_count,
                    drift_collector=drift_collector,
                )
            )

        # ═══ Phase 2 → 3 transition: DESIGN_REVIEW → CODE_SYNTHESIS ═══
        # Boundary invariant (AC2): the design phase exit is structurally
        # gated on ``design_outcome.ready``, which is True only when
        # ``SpecReadinessGate`` passed (no critical failures) AND the
        # ``DesignReviewAgent`` marked the spec ready. The short-circuit
        # branch above returns before reaching this point, so reaching
        # this line implies the gate has passed for this design attempt.
        assert design_outcome.ready, (
            "DESIGN_REVIEW → CODE_SYNTHESIS boundary invariant violated: "
            "design_outcome.ready is False but the short-circuit branch "
            "did not return. This is a bug in _run_design_attempt."
        )
        _emit_phase_transition(
            emit,
            from_phase=Phase.DESIGN_REVIEW,
            to_phase=Phase.CODE_SYNTHESIS,
            spec=spec,
            code="",
            attempt=design_attempt,
        )
        return _DesignPhaseResult(
            record=None, spec=spec, rationale=rationale, design_context=design_context
        )

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

    def _orchestrate_refinement_and_alignment(
        self,
        *,
        spec: StrategySpec,
        code: str,
        config: BacktestConfig,
        original_spec: StrategySpec,
        original_code: str,
        rationale: str,
        all_gate_results: List[QualityGateResult],
        refinement_attempts: List[str],
        zero_trade_attempts: List[str],
        emit: PhaseCallback,
        design_attempt: int,
        phase_back_count: int,
        drift_collector: _DriftCollector,
        design_context: _DesignPersistContext,
    ) -> _RefinementAlignmentResult:
        """Run pre-synthesis gating, the refinement loop, and trade alignment.

        Pre: ``code`` is the freshly synthesized strategy code; the running
        lists (``all_gate_results`` / ``refinement_attempts`` /
        ``zero_trade_attempts``) are mutated in place by the sub-loops.
        Post: returns a ``_RefinementAlignmentResult``. A critical pre-synthesis
        spec failure short-circuits via ``record`` (caller returns it).
        Otherwise ``record`` is ``None`` and the returned ``synthesis`` /
        ``alignment`` bundles carry every downstream field. Emits the
        CODE_SYNTHESIS → BACKTEST_AND_VERIFICATION boundary and the
        backtest-cache telemetry.
        Raises: ``SpecImplementabilityError`` (from the refinement loop) with
        this attempt's ``design_context`` attached when the raiser left it unset.
        """
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
            phase_back_count=phase_back_count,
            drift_collector=drift_collector,
            design_context=design_context,
        )
        if pre_synthesis is not None:
            return _RefinementAlignmentResult(record=pre_synthesis)

        # ── Phase 2: CODE REFINEMENT LOOP ─────────────────────────────
        # ``_run_synthesis_loop`` iterates up to ``MAX_CODE_REFINEMENT_ROUNDS``
        # rounds of (validate → fetch → execute → trade-collect → evaluate)
        # and either converges (``execution_succeeded=True``) or
        # short-circuits with ``max_rounds_exhausted`` / a fatal-fetch flag.
        # The loop appends to ``all_gate_results``, ``refinement_attempts``,
        # and ``zero_trade_attempts`` in-place; the returned outcome carries
        # the final spec/code/trades/metrics + universe audit.
        try:
            synthesis = self._run_synthesis_loop(
                spec=spec,
                code=code,
                config=config,
                all_gate_results=all_gate_results,
                refinement_attempts=refinement_attempts,
                zero_trade_attempts=zero_trade_attempts,
                emit=emit,
                drift_collector=drift_collector,
            )
        except SpecImplementabilityError as exc:
            # The synthesis refinement loop tripped re-design. Attach this
            # attempt's design-loop telemetry to the exception (mirroring the
            # ``drift_collector`` hand-off) so the outer re-entry-exhaustion
            # short-circuit in ``run_cycle`` persists the generation-funnel
            # telemetry of the design loop that actually ran, rather than an
            # empty default. Only set when a raiser didn't already supply one.
            if exc.design_context is None:
                exc.design_context = design_context
            raise
        # Synthesis fields needed locally for the phase boundary + alignment
        # call; the caller reads the remaining fields off the returned bundle.
        spec = synthesis.spec
        code = synthesis.code
        trades = synthesis.trades
        metrics = synthesis.metrics
        market_data = synthesis.market_data
        execution_succeeded = synthesis.execution_succeeded

        # ═══ Phase 3 → 4 transition: CODE_SYNTHESIS → ═════════════════
        # ═══                         BACKTEST_AND_VERIFICATION ════════
        # Boundary invariant (AC3): synthesis advancing with
        # ``execution_succeeded=True`` structurally requires that
        # ``CodeConformanceGate.check`` passed in the final round (it is
        # one of the critical gates the refinement loop must clear before
        # executing). When synthesis short-circuits (max-rounds exhausted
        # or fatal fetch failure), we still cross into the verification
        # phase but the boundary event reflects the un-converged code
        # hash and downstream gates handle the rest.
        _emit_phase_transition(
            emit,
            from_phase=Phase.CODE_SYNTHESIS,
            to_phase=Phase.BACKTEST_AND_VERIFICATION,
            spec=spec,
            code=code,
            attempt=design_attempt,
        )

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
            ran_on_non_conforming_code=synthesis.ran_on_non_conforming_code,
            drift_collector=drift_collector,
        )
        if alignment_outcome.rejection_reason:
            logger.info(
                "Alignment loop for %s ended with rejection_reason=%s",
                alignment_outcome.spec.strategy_id,
                alignment_outcome.rejection_reason,
            )

        # Backtest-cache effectiveness for this attempt — emitted so the
        # synthesis/alignment re-execution savings are observable post hoc.
        _bt_cache = getattr(self, "_backtest_cache", None)
        if _bt_cache is not None:
            emit(
                "telemetry",
                {
                    "kind": "backtest_cache",
                    "hits": _bt_cache.hits,
                    "misses": _bt_cache.misses,
                },
            )
            logger.info(
                "backtest_cache for %s: hits=%d misses=%d",
                alignment_outcome.spec.strategy_id,
                _bt_cache.hits,
                _bt_cache.misses,
            )

        return _RefinementAlignmentResult(
            record=None, synthesis=synthesis, alignment=alignment_outcome
        )

    def _orchestrate_verification_and_analysis(
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
        runtime_lookahead_violation: bool,
        open_position_entry_reasons: List[str],
        refinement_attempts: List[str],
        rationale: str,
        emit: PhaseCallback,
    ) -> Tuple[BacktestResult, bool, str]:
        """Count the trial, run verification, and generate the analysis.

        Pre: the refinement + alignment loops have settled the run state.
        Post: returns ``(metrics, is_winning, narrative)``. Increments the
        convergence trial counter (one per refinement round, plus the first),
        runs ``_run_verification_phase`` (which mutates ``metrics`` /
        ``all_gate_results`` and resolves ``is_winning``), and produces the
        analysis narrative off the conformance-resolved alignment report.
        """
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
            runtime_lookahead_violation=runtime_lookahead_violation,
            emit=emit,
            open_position_entry_reasons=open_position_entry_reasons,
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
        return metrics, is_winning, narrative

    def _extract_findings_and_assemble_record(
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
        provider_used: Dict[str, str],
        max_rounds_exhausted: bool,
        execution_succeeded: bool,
        is_winning: bool,
        trades_aligned: bool,
        refinement_attempts: List[str],
        alignment_rounds: int,
        all_gate_results: List[QualityGateResult],
        ran_on_non_conforming_code: bool,
        design_context: _DesignPersistContext,
        alignment_reports: List[TradeAlignmentReport],
        phase_back_count: int,
        drift_collector: _DriftCollector,
        emit: PhaseCallback,
    ) -> StrategyLabRecord:
        """Extract the final alignment findings and assemble the record.

        Pre: all phases have completed; ``alignment_reports`` holds one report
        per alignment iteration (empty when the loop never ran).
        Post: returns the persisted ``StrategyLabRecord`` built by
        ``_assemble_record``, carrying the last report's per-rule findings (or
        an empty list) and ``refinement_rounds = len(refinement_attempts)``.
        """
        # Final-iteration per-rule findings from the deterministic
        # alignment gate. The orchestrator's loop produces one report
        # per iteration; the last one carries the ledger as it stood
        # against the known-good code/trades that ``trades_aligned``
        # was computed from. When the alignment loop never ran (no
        # market_data, no trades) the list is empty.
        alignment_findings: List[AlignmentFinding] = (
            list(alignment_reports[-1].alignment_findings) if alignment_reports else []
        )

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
            provider_used=provider_used,
            max_rounds_exhausted=max_rounds_exhausted,
            execution_succeeded=execution_succeeded,
            is_winning=is_winning,
            trades_aligned=trades_aligned,
            refinement_rounds=len(refinement_attempts),
            alignment_rounds=alignment_rounds,
            all_gate_results=all_gate_results,
            emit=emit,
            ran_on_non_conforming_code=ran_on_non_conforming_code,
            design_context=design_context,
            alignment_findings=alignment_findings,
            phase_back_count=phase_back_count,
            drift_collector=drift_collector,
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
        drift_collector: Optional[_DriftCollector] = None,
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
        return new_spec, new_code, False

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
        design_context: Optional[_DesignPersistContext] = None,
        phase_back_count: int = 0,
        drift_collector: Optional[_DriftCollector] = None,
    ) -> StrategyLabRecord:
        """Persist a failed cycle that exited before code execution.

        Used by the pre-synthesis spec gate so that critical spec failures
        short-circuit without ever running ``run_strategy_code`` or
        fetching market data. The record still flows through
        ``convergence_tracker`` so failed specs influence diversity
        directives on the next cycle.
        """
        now_iso = datetime.now(timezone.utc).isoformat()

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

        design_context = design_context or _DesignPersistContext()
        dc = drift_collector or _DriftCollector()
        gate_timeline = dc.gate_timeline + [
            GateEvent(
                phase=g.phase,
                gate_name=g.gate_name,
                passed=g.passed,
                severity=g.severity,
                details=g.details,
                timestamp=g.evaluated_at,
            )
            for g in all_gate_results
        ]
        lab_record_id = f"lab-{uuid.uuid4().hex[:8]}"
        # A short-circuit record never executed a backtest, so it never ran on
        # non-conforming code (the flag defaults False on both telemetry and
        # the record field).
        loop_telemetry = _finalize_loop_telemetry(design_context, all_gate_results, spec, code)
        record = StrategyLabRecord(
            lab_record_id=lab_record_id,
            strategy=spec,
            backtest=backtest_record,
            is_winning=False,
            strategy_rationale=rationale,
            analysis_narrative=short_circuit_reason,
            created_at=now_iso,
            refinement_rounds=len(refinement_attempts),
            design_rounds=design_context.rounds,
            critiques=[c.model_dump() for c in design_context.critiques],
            quality_gate_results=[g.model_dump() for g in all_gate_results],
            strategy_code=code,
            original_spec=original_spec,
            original_code=original_code,
            spec_implementability_phase_backs=phase_back_count,
            spec_history=list(dc.spec_history),
            code_history=list(dc.code_history),
            gate_timeline=gate_timeline,
            loop_telemetry=loop_telemetry,
            ran_on_non_conforming_code=False,
        )

        # Short-circuited cycles never reached a backtest, and ``spec`` may
        # carry a coerced placeholder asset_class (an unsupported class like
        # ``bonds`` is canonicalized to ``stocks`` for schema validity before
        # the redesign route). Record the signature/failure modes for stall and
        # failure-frequency detection, but keep the placeholder out of the
        # diversity history so it can't emit a false "heavily stocks" steering
        # directive on the next cycle.
        self.convergence_tracker.record(spec, all_gate_results, count_asset_class=False)

        emit(
            "complete",
            {
                "record_id": lab_record_id,
                "is_winning": False,
                "metrics": backtest_record.result.model_dump(),
                "refinement_rounds": len(refinement_attempts),
                "short_circuit": short_circuit_status,
                "phase_back_count": phase_back_count,
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
            single = self.market_data_service.fetch_multi_symbol_range(
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
# ──────────────────────────────────────────────────────────────────────────
from ._orchestrator_helpers import (  # noqa: E402  — keep at file end
    _AlignmentLoopOutcome,
    _AlignmentRoundOutcome,
    _AnomalyRecoveryOutcome,
    _apply_veto_to_acceptance_reason,
    _attach_execution_diagnostics,
    _build_rule_implementation_map,
    _closes_to_equity,
    _CodeSynthesisPhaseResult,
    _daily_returns_from_trades,
    _DesignLoopOutcome,
    _DesignPersistContext,
    _DesignPhaseResult,
    _DriftCollector,
    _equity_to_returns,
    _format_execution_diagnostics,
    _MarketDataFetch,
    _maybe_attach_coverage_report,
    _merge_risk_limits_tighten_only,
    _parse_bar_date,
    _RefinementAlignmentResult,
    _resolve_vix_provider,
    _SynthesisEvaluateResult,
    _SynthesisFetchResult,
    _SynthesisLoopOutcome,
    _VerificationOutcome,
)
