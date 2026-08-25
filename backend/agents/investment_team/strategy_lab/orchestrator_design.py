"""DesignMixin — the design <-> review loop cluster extracted from
:mod:`orchestrator`.

Pure move (issue #1732, PR 1 of 6 decomposing the StrategyLabOrchestrator
god-class tracking issue): every method and helper below is relocated
verbatim from ``orchestrator.py``. No behavior changes. ``DesignMixin`` is
mixed into ``StrategyLabOrchestrator`` — see the class statement in
``orchestrator.py`` for the current base order (more mixins have since
joined it); its methods expect the attributes
``StrategyLabOrchestrator.__init__`` sets on ``self`` (``self.design_agent``,
``self.design_review_agent``, ``self.spec_readiness_gate``,
``self.strategy_validator``, plus the ``self.record_gates`` /
``self._build_short_circuit_record`` / ``self._synthesize_initial_code`` /
``self._run_pre_synthesis_phase`` / ``self._run_synthesis_loop`` /
``self._run_trade_alignment_loop`` / ``self._run_verification_phase`` /
``self._run_analysis_phase`` / ``self._assemble_record`` methods) — all of
which stay on the base class or another mixin and resolve via MRO on the
final composed instance.

``_orchestrate_refinement_and_alignment``, ``_orchestrate_verification_and_analysis``,
``_extract_findings_and_assemble_record``, and the module-level
``_resolve_alignment_report_for_analysis`` moved here from ``orchestrator.py``
because ``_run_design_attempt``, their sole caller, already lives in this
file. They still call across into ``SynthesisMixin`` / ``AlignmentMixin`` /
``VerificationMixin`` / ``RecordAssemblyMixin`` methods via ``self``, which
resolves fine through the MRO regardless of which file defines the caller.
See ``MIXIN_BOUNDARIES.md`` for the full rationale.

This module must not import anything from ``orchestrator.py`` (that would be
circular: ``orchestrator.py`` imports ``DesignMixin`` from here before its
own class statement executes). Pure helpers shared across this cluster, the
other extracted mixins, and code that stays in ``orchestrator.py`` live in
``_orchestrator_helpers.py`` instead (see ``_env_flag``,
``_emit_phase_transition``).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from pydantic import ValidationError

from ..market_data_service import OHLCVBar
from ..models import (
    BacktestConfig,
    BacktestResult,
    ExpectancyForecast,
    StrategyLabRecord,
    StrategySpec,
)
from ..signal_intelligence_models import SignalIntelligenceBriefV1
from ..strategy_lab_context import (
    PROMPT_ASSET_CLASSES,
    filter_records_by_asset_class,
    normalize_asset_class,
    normalize_asset_class_strict,
    select_asset_category,
    select_signal_brief,
)
from ._orchestrator_helpers import (
    _DesignAttemptState,
    _DesignPersistContext,
    _DriftCollector,
    _emit_phase_transition,
    _env_flag,
    _has_critical_failures,
    _RefinementAlignmentResult,
)
from .agents._llm_budget import DesignBudgetExhausted, _annotate_budget_exhaustion, active_budget
from .agents.alignment import AlignmentIssue, TradeAlignmentReport
from .agents.design_review import CritiqueIssue, CritiqueLedger, LedgerDelta, SpecCritique
from .alignment_findings import AlignmentFinding
from .backtest_cache import BacktestCache
from .budget_config import StrategyLabBudgetConfig
from .exceptions import OrchestratorContractError, SpecImplementabilityError
from .market_regime import RegimeSummary, filter_regime_summary
from .mechanical_repair import RepairAction, demote_code_path, repair_spec, select_code_path
from .phases import Phase
from .quality_gates.convergence_tracker import is_asset_class_steering_directive
from .quality_gates.models import QualityGateResult
from .spec_dsl import DEFAULT_SIZING_PAYLOAD

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


def build_spec_from_dict(
    strategy_dict: Dict[str, Any],
    *,
    strategy_id: str,
    default_asset_class: str = "stocks",
) -> "StrategySpec":
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
    strict ``StrategySpec`` validator. A missing or blank ``asset_class``
    resolves to ``default_asset_class`` rather than being treated as
    unsupported, so it never triggers a spurious redesign.

    ``default_asset_class`` is the category the caller's design attempt is
    pinned to. Filling an *omitted* field from the pin is not a relabel: the
    payload expressed no choice, and the pin is the only correct value it
    could have had. A payload that names a *different* class is left exactly
    as authored — readiness Rule 11 then rejects it so the strategy gets
    rebuilt for the pinned category rather than silently reassigned to it.

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
    assert default_asset_class in PROMPT_ASSET_CLASSES, (
        "default_asset_class must be a canonical PROMPT_ASSET_CLASSES member"
    )
    # ``or`` alone would let a whitespace-only value through as an authored
    # choice; a blank field expresses no choice, so it inherits the pin.
    raw_asset_class = str(strategy_dict.get("asset_class") or "").strip() or default_asset_class
    asset_class = normalize_asset_class(raw_asset_class)
    # A missing/blank asset_class is the documented default, which
    # ``normalize_asset_class`` already produced above — it is not an unsupported
    # class, so it must not trip the strict validator and force a redesign. Only a
    # genuinely-named-but-unknown class (e.g. ``bonds``) routes to redesign. The
    # strict check runs on the RAW value so an unknown class is caught before
    # ``normalize_asset_class`` flattens it to ``stocks``; checking the coerced
    # value would let ``bonds`` pass as ``stocks`` and be backtested under the
    # wrong universe/gates.
    unsupported_class = False
    if str(raw_asset_class or "").strip():
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
        risk_limits=strategy_dict.get("risk_limits") or {},
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
    return StrategyLabBudgetConfig.from_env().design_review_rounds


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
    return StrategyLabBudgetConfig.from_env().design_review_stall_rounds


def _mechanical_repair_enabled() -> bool:
    """Resolve the deterministic mechanical-repair pre-flight toggle.

    Pre: none.
    Post: returns ``True`` unless ``STRATEGY_LAB_MECHANICAL_REPAIR_ENABLED`` is
    set to a recognised falsey value. Accepted truthy values are
    ``true``/``1``/``yes`` (case-insensitive); anything else disables the
    pre-flight and restores the pure LLM-revise behaviour. Default ``true``.
    """
    return _env_flag("STRATEGY_LAB_MECHANICAL_REPAIR_ENABLED")


def _demote_compilable_custom_code_enabled() -> bool:
    """Resolve the "demote a compilable custom-code spec to Path A" toggle.

    Pre: none.
    Post: returns ``True`` unless ``STRATEGY_LAB_DEMOTE_COMPILABLE_CUSTOM_CODE`` is
    set to a recognised falsey value. Default ``true`` — a spec the LLM flagged as
    custom code but which compiles cleanly is over-elected onto the drift-prone
    custom path, so it is demoted to the faithful compiled path. An operator can
    disable this if a lossy-but-compilable spec is ever wrongly demoted.
    """
    return _env_flag("STRATEGY_LAB_DEMOTE_COMPILABLE_CUSTOM_CODE")


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


@dataclass
class _DesignLoopOutcome:
    """Bundle returned by ``_run_design_loop``.

    The design loop iterates (DesignAgent → SpecReadinessGate →
    DesignReviewAgent → DesignAgent.revise) until either the reviewer
    marks the spec ready or the configured round budget is exhausted.

    Invariants on return:
    - ``ready=True`` ⇒ ``spec`` passed both the deterministic readiness
      gate and the LLM reviewer on the most recent round; downstream
      code synthesis may proceed.
    - ``ready=False`` ⇒ the round budget exhausted; ``critique_history``
      carries one entry per round (synthetic critiques produced from
      readiness findings count); the orchestrator must short-circuit
      the cycle rather than running code against a not-ready spec.
    - ``rounds`` equals ``len(critique_history)`` in both branches.
    - ``spec`` is the final candidate the loop produced (whether ready
      or not), so the audit trail always carries the spec the cycle
      stopped on.
    - ``budget_exhausted=True`` ⇒ ``ready=False`` and the per-cycle
      LLM-call budget was hit mid-loop; ``spec`` / ``critique_history``
      carry whatever state existed at the trip. It disambiguates the two
      reasons a ``ready=False`` outcome can arise: round-budget exhaustion
      (``False``) versus LLM-call-budget exhaustion (``True``), which the
      orchestrator maps to ``failed: design_not_ready`` and
      ``failed: budget_exhausted`` respectively.
    """

    spec: StrategySpec
    rationale: str
    ready: bool
    rounds: int
    # NB: typed as ``Any`` to avoid a cycle with ``agents/design_review``.
    # The orchestrator passes a ``List[SpecCritique]`` through.
    critique_history: List[Any]
    budget_exhausted: bool = False
    # Why the loop stopped: "ready" | "round_cap" | "stalled" |
    # "budget_exhausted". Disambiguates the not-ready short-circuit status
    # (stall vs honest round-cap exhaustion).
    stop_reason: str = ""
    # Design-loop slice of the persisted telemetry summary (round count,
    # stop reason, critique-ledger totals). Gate counts + the
    # compiled-vs-custom flag are merged in at record-build time.
    loop_telemetry: Dict[str, Any] = field(default_factory=dict)


@dataclass
class _DesignPhaseResult:
    """Return envelope for ``_run_design_attempt``'s design + review phase.

    ``record`` is a short-circuit ``StrategyLabRecord`` (typed ``Any`` to avoid
    an import cycle) when the design loop did not reach readiness — the caller
    returns it immediately. Otherwise ``record`` is ``None`` and the
    ``spec`` / ``rationale`` / ``design_context`` carry the converged design
    forward into code synthesis.
    """

    record: Optional[Any]
    spec: Optional[StrategySpec] = None
    rationale: str = ""
    design_context: Optional[_DesignPersistContext] = None


class DesignMixin:
    def _run_design_loop(
        self,
        *,
        prior_records: List[StrategyLabRecord],
        signal_briefs: Optional[Dict[str, SignalIntelligenceBriefV1]],
        directives: List[str],
        exclude_asset_classes: Optional[List[str]],
        config: BacktestConfig,
        all_gate_results: List[QualityGateResult],
        emit: PhaseCallback,
        design_attempt: int = 0,
        drift_collector: Optional[_DriftCollector] = None,
        regime_summary: Optional[RegimeSummary] = None,
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
        if max_rounds < 1:
            raise ValueError("design-review round cap must be ≥ 1")

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

        # ── Asset-category pin ───────────────────────────────────────────
        # Every design attempt commits to exactly ONE asset category, chosen
        # at random from the categories the user selected at run start
        # (``exclude_asset_classes`` is the complement of that selection
        # within ``PROMPT_ASSET_CLASSES``; an unrestricted run means every
        # category is allowed, not that no pin applies).
        #
        # The pin is unconditional because asset categories are not
        # interchangeable: forex microstructure, crypto's 24/7 sessions, and
        # equity market hours make a "learning" drawn from one category
        # actively misleading in another. An attempt that reasoned over a
        # blended cross-category history would produce conclusions that hold
        # for no category in particular, whether or not the user narrowed the
        # menu. So every attempt gets one category and sees only that
        # category's evidence.
        #
        # Three things follow from the pin, all of them hard restrictions
        # rather than prompt-level requests:
        #   * ``prior_records`` is filtered to the pinned category, so the
        #     "Prior Strategy Results" analysis cannot reference another one.
        #   * ``exclude_asset_classes`` handed to the designer forbids every
        #     other class, rather than leaving it free among the allowed set.
        #   * The readiness gate enforces the pin (Rule 11), so a spec that
        #     drifts off-category can never be marked ready and therefore can
        #     never reach code synthesis or be persisted as a strategy.
        #
        # The convergence tracker's diversity directive separately tells the
        # designer to avoid whichever asset class is over-represented in
        # recent history. Bias the pin away from that same set so the two
        # steering mechanisms don't contradict each other — see the
        # ``directives`` filtering right after selection.
        diversity_avoid_classes = self.convergence_tracker.get_diversity_avoid_classes()
        selected_category: str = select_asset_category(
            exclude_asset_classes, avoid=diversity_avoid_classes
        )
        category_prior_records = filter_records_by_asset_class(prior_records, selected_category)
        pinned_exclude_asset_classes = [c for c in PROMPT_ASSET_CLASSES if c != selected_category]
        # Scope the two cross-category analysis surfaces the designer is also
        # handed. Both are computed once per batch/cycle over every asset
        # class; without this the designer reads a signal brief whose
        # "evidence from priors" and diversity hint span categories it is
        # forbidden to use, and a regime block quoting four irrelevant markets.
        category_signal_brief = select_signal_brief(signal_briefs, selected_category)
        category_regime_summary = filter_regime_summary(regime_summary, selected_category)

        # A pinned attempt cannot satisfy ANY "change asset class" directive —
        # ``pinned_exclude_asset_classes`` already forbids every class but the
        # pinned one — so drop them all rather than hand the designer a
        # self-contradictory prompt ("use something other than X" alongside
        # "MANDATORY EXCLUSION: only X is allowed"). This covers the stall
        # directive as well as the diversity one; the predicate lives with the
        # text it matches (see ``convergence_tracker``) so a reword cannot
        # silently stop it matching.
        attempt_directives = [d for d in directives if not is_asset_class_steering_directive(d)]

        try:
            strategy_dict, rationale = self.design_agent.run(
                prior_records=category_prior_records,
                signal_brief=category_signal_brief,
                convergence_directives=attempt_directives or None,
                exclude_asset_classes=pinned_exclude_asset_classes,
                regime_summary=category_regime_summary,
            )
            spec = self._build_spec_from_dict(
                strategy_dict, strategy_id=strategy_id, default_asset_class=selected_category
            )
            # No post-hoc correction here: a spec that came back off-category
            # is caught by readiness Rule 11 on the very first round below and
            # answered by that round's own ``revise`` call.

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
                selected_category=selected_category,
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
                spec = self._build_spec_from_dict(
                    {}, strategy_id=strategy_id, default_asset_class=selected_category
                )
            # The spec is left exactly as the designer produced it, even when
            # it is off-pin. This exit is a *failure* record
            # (``status="failed: budget_exhausted"``) that never reaches
            # synthesis, so relabelling its ``asset_class`` to the pinned
            # category would only file crypto logic under stocks in the
            # record store — where the next attempt's category-scoped
            # prior-record filter would then read it as stocks evidence.
            # ``asset_category`` in the telemetry below records what this
            # attempt was pinned to; ``spec.asset_class`` stays honest about
            # what was actually produced.
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
            budget_telemetry["asset_category"] = selected_category
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
        selected_category: str,
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
                    pinned_asset_class=selected_category,
                )
            )

            # Step 2 — deterministic mechanical pre-flight (repair criticals,
            # then trial-compile a readiness-clean spec to pick the code path)
            # before every review round.
            repair_count_before_round = mechanical_repair_count
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
                    pinned_asset_class=selected_category,
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

            # Step 5 — revise the spec from this round's critique. Skip the
            # designer's internal self-review LLM call when this round's
            # readiness gate passed AND no mechanical repair fired this round
            # — the spec is already known structurally clean, so the
            # self-review audit would be redundant work.
            repair_fired_this_round = mechanical_repair_count > repair_count_before_round
            skip_self_review = deterministic_ready and not repair_fired_this_round
            spec, rationale = self._revise_with_regression_notice(
                spec=spec,
                rationale=rationale,
                critique=critique,
                delta=delta,
                critique_history=critique_history,
                strategy_id=strategy_id,
                mechanical_repair_count=mechanical_repair_count,
                drift_collector=drift_collector,
                skip_self_review=skip_self_review,
                default_asset_class=selected_category,
            )

        loop_telemetry = _design_loop_telemetry_summary(
            ledger, len(critique_history), stop_reason, mechanical_repair_count
        )
        loop_telemetry["asset_category"] = selected_category
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
        pinned_asset_class: Optional[str] = None,
    ) -> Tuple[List[QualityGateResult], Optional[tuple], bool]:
        """Run the deterministic readiness gate, memoized on the spec signature.

        Pre: ``readiness_results`` / ``last_readiness_signature`` carry the
        previous round's verdict and the spec signature that produced it;
        ``pinned_asset_class`` is the category this design attempt is pinned
        to (readiness Rule 11), or ``None`` outside a pinned attempt.
        Post: returns ``(readiness_results, last_readiness_signature,
        deterministic_ready)``. Re-validates (and records the gates onto
        ``all_gate_results`` in place) only when the spec's readiness-relevant
        signature changed since ``last_readiness_signature`` — the gate would
        otherwise return the same verdict. ``deterministic_ready`` is ``True``
        iff no readiness critical is present, so a spec violating the pin is
        never reported ready.

        The memoization stays sound under a pin because ``asset_class`` and
        ``target_symbols`` — the only two fields Rule 11 reads — are both part
        of ``_spec_readiness_signature``, and the pin itself is fixed for the
        whole attempt.
        """
        signature = _spec_readiness_signature(spec)
        if signature != last_readiness_signature:
            readiness_results = self.spec_readiness_gate.validate(
                spec,
                phase="design",
                backtest_config=config,
                pinned_asset_class=pinned_asset_class,
            )
            self.record_gates(readiness_results, all_gate_results, refinement_round=-1)
            last_readiness_signature = signature
        deterministic_ready = not _has_critical_failures(readiness_results)
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
        pinned_asset_class: Optional[str] = None,
        all_gate_results: List[QualityGateResult],
        drift_collector: Optional[_DriftCollector],
        emit: PhaseCallback,
    ) -> Tuple[StrategySpec, List[QualityGateResult], Optional[tuple], bool, int]:
        """Run the deterministic mechanical pre-flight in two ordered stages.

        Stage 1 repairs mechanical readiness criticals (timeframe data
        availability, position-cap bound, and — under a pin — stray
        off-category ``target_symbols``) so they never cost an LLM ``revise``
        round, then re-validates. The *class* half of the category pin is
        deliberately not repairable here: a spec declaring the wrong category
        needs its logic rebuilt, not its label rewritten, so it stays a
        readiness critical for this round's ``revise`` call to answer. Stage 2 — only once the spec is readiness-
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
        outcome = repair_spec(spec, config=config, pinned_asset_class=pinned_asset_class)
        if outcome.actions:
            pre_repair_spec = spec.model_copy(deep=True)
            spec = outcome.spec
            repair_actions.extend(outcome.actions)
            # Re-validate only when a repair changed a readiness-relevant
            # field (mechanical repairs always do; the signature catches it).
            repaired_signature = _spec_readiness_signature(spec)
            if repaired_signature != last_readiness_signature:
                readiness_results = self.spec_readiness_gate.validate(
                    spec,
                    phase="design",
                    backtest_config=config,
                    pinned_asset_class=pinned_asset_class,
                )
                self.record_gates(readiness_results, all_gate_results, refinement_round=-1)
                last_readiness_signature = repaired_signature
                deterministic_ready = not _has_critical_failures(readiness_results)

        # Stage 2 — trial compile, only on a readiness-clean spec.
        if deterministic_ready:
            # Promote: a spec outside the compiler envelope flips ON to custom code.
            compile_action = select_code_path(spec)
            # Demote: a custom-code spec that compiles cleanly was over-elected onto
            # the drift-prone custom path — flip it OFF back to the faithful compiled
            # path. Mutually exclusive with promotion (a spec is one or the other).
            if compile_action is None and _demote_compilable_custom_code_enabled():
                compile_action = demote_code_path(spec)
            if compile_action is not None:
                if pre_repair_spec is None:
                    pre_repair_spec = spec.model_copy(deep=True)
                spec = spec.model_copy(update={"requires_custom_code": compile_action.after})
                repair_actions.append(compile_action)
                # Flipping ``requires_custom_code`` can change the
                # readiness verdict (it gates which closed-form gates
                # apply), so re-validate against the flipped spec rather
                # than ride the stale pre-flip verdict.
                repaired_signature = _spec_readiness_signature(spec)
                if repaired_signature != last_readiness_signature:
                    readiness_results = self.spec_readiness_gate.validate(
                        spec,
                        phase="design",
                        backtest_config=config,
                        pinned_asset_class=pinned_asset_class,
                    )
                    self.record_gates(readiness_results, all_gate_results, refinement_round=-1)
                    last_readiness_signature = repaired_signature
                    deterministic_ready = not _has_critical_failures(readiness_results)

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
            # Surface the hypothesis-vs-rules consistency finding to the reviewer so
            # a narrative/DSL mismatch is reconciled during the design loop (the
            # reviewer adjudicates and can require a revise) rather than only being
            # recorded as a pre-synthesis warning after the loop has converged. The
            # reviewer's findings list is a fresh merge — ``readiness_results`` is
            # left untouched for the memoization / recording paths.
            reviewer_findings = list(
                readiness_results
            ) + self.strategy_validator.check_hypothesis_rules(spec, phase="design")
            try:
                critique = self.design_review_agent.run(
                    spec,
                    reviewer_findings,
                    prior_critiques=critique_history,
                )
            except DesignBudgetExhausted as exc:
                # The budget handler in ``_run_design_loop`` only captures
                # this helper's spec/rationale/counters on the success-return
                # path; surface the latest in-loop spec (post mechanical-
                # repair) and the repair count so the short-circuit record
                # reflects the spec actually evaluated, not the pre-loop
                # draft, and its telemetry still reports the repairs applied.
                _annotate_budget_exhaustion(
                    exc,
                    spec,
                    rationale=rationale,
                    mechanical_repair_count=mechanical_repair_count,
                )
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
        skip_self_review: bool = False,
        default_asset_class: str = "stocks",
    ) -> Tuple[StrategySpec, str]:
        """Revise the spec from the round's critique, flagging regressions.

        Pre: this round did not stop the loop (not ready / stalled / capped).
        Post: returns ``(spec, rationale)`` — the revised candidate built from
        the designer's payload and its new rationale. Any regression (an issue
        resolved earlier that reappeared) is fed back to the designer as an
        explicit "do not reintroduce" notice (advisory, not a hard block — a
        hard block risks deadlock if the model cannot avoid it). Records the
        spec drift when a collector is present.
        ``skip_self_review`` (default ``False``) is forwarded verbatim to
        :meth:`DesignAgent.revise` — the caller (``_run_design_review_rounds``)
        computes it as "this round's readiness gate passed AND no mechanical
        repair fired this round", so a structurally clean revision skips the
        designer's internal self-review LLM call.
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
                skip_self_review=skip_self_review,
            )
        except DesignBudgetExhausted as exc:
            _annotate_budget_exhaustion(
                exc,
                spec,
                rationale=rationale,
                mechanical_repair_count=mechanical_repair_count,
            )
            raise
        spec = self._build_spec_from_dict(
            strategy_dict, strategy_id=strategy_id, default_asset_class=default_asset_class
        )
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
        self,
        strategy_dict: Dict[str, Any],
        *,
        strategy_id: str,
        default_asset_class: str = "stocks",
    ) -> StrategySpec:
        """Thin instance wrapper over :func:`build_spec_from_dict`.

        Retained so the orchestrator's existing call sites keep their method
        form; the construction logic lives in the module-level function so it
        can be unit-tested without instantiating the orchestrator.
        """
        return build_spec_from_dict(
            strategy_dict, strategy_id=strategy_id, default_asset_class=default_asset_class
        )

    def _run_design_attempt(
        self,
        *,
        prior_records: List[StrategyLabRecord],
        config: BacktestConfig,
        signal_briefs: Optional[Dict[str, SignalIntelligenceBriefV1]],
        emit: PhaseCallback,
        exclude_asset_classes: Optional[List[str]],
        directives: List[str],
        design_attempt: int = 0,
        phase_back_count: int = 0,
        drift_collector: Optional[_DriftCollector] = None,
        cumulative_gate_results: Optional[List[QualityGateResult]] = None,
        regime_summary: Optional[RegimeSummary] = None,
        resume_spec: Optional[StrategySpec] = None,
        resume_rationale: Optional[str] = None,
        resume_design_context: Optional[_DesignPersistContext] = None,
        checkpoint_hook: Optional[PhaseCallback] = None,
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

        Each phase helper below owns its own short-circuiting and its own
        exit-boundary phase-transition emission; this method only sequences
        them and checks ``.record`` after each call.

        Checkpoint resume (``ADR-012``): ``resume_spec``/``resume_rationale``/
        ``resume_design_context`` let a caller skip Phase 1 (design + review)
        entirely when it already has that phase's converged output from a
        prior, crashed execution of this exact attempt -- ``resume_spec is
        not None`` if and only if ``resume_design_context is not None``
        (``resume_rationale`` may independently be ``None``/``""``).
        ``checkpoint_hook``, when not ``None``, is invoked exactly once,
        immediately after Phase 1 converges on a non-resumed run (never on
        the short-circuit/not-ready path, nor when resuming, since Phase 1
        didn't run), as ``checkpoint_hook("design_synthesis_boundary",
        {"spec": spec, "rationale": rationale, "design_context":
        design_context})`` -- deliberately the live Python objects, not a
        JSON-shaped dict, since its only intended consumer
        (``temporal.activities.run_design_attempt_activity``) lives in the
        same process and does its own wire-conversion before persisting.
        Both default to ``None``, so thread mode's caller (which passes
        neither) is unaffected.

        Preconditions:
            ``resume_spec is None`` if and only if ``resume_design_context
            is None``.
        """
        if (resume_spec is None) != (resume_design_context is None):
            raise ValueError(
                "resume_spec and resume_design_context must both be set or both be None"
            )
        # Reset per-attempt counters so a re-entry starts fresh.
        self._consecutive_spec_mutation_rounds = {}
        # Fresh, attempt-scoped backtest memo. Discarding it per attempt keeps
        # a cached result from ever crossing a market-data snapshot: the same
        # code re-run against the same hoisted ``market_data`` + ``config``
        # (alignment re-checks, determinism re-checks, audit re-backtests)
        # short-circuits to the stored ``StrategyRunResult``.
        self._backtest_cache = BacktestCache()
        # Fresh, attempt-scoped benchmark-bars memo — same rationale as
        # ``_backtest_cache`` above, so a re-entry never reuses a benchmark
        # fetch across attempts.
        self._benchmark_bars_cache = {}
        # Fresh, attempt-scoped anomaly-check memo (see
        # ``_check_anomalies_cached``) — reused across the synthesis loop and
        # the subsequent alignment loop within this attempt, discarded on
        # re-entry so a re-entry never reuses a verdict across attempts.
        self._last_anomaly_check: Optional[Tuple[str, List[QualityGateResult]]] = None

        all_gate_results: List[QualityGateResult] = (
            cumulative_gate_results if cumulative_gate_results is not None else []
        )
        refinement_attempts: List[str] = []
        zero_trade_attempts: List[str] = []
        if drift_collector is None:
            drift_collector = _DriftCollector()

        # ── Phase 1: DESIGN + REVIEW LOOP ──────────────────────────────
        if resume_spec is not None:
            # Checkpoint resume: Phase 1 already ran (and was durably
            # checkpointed) by a prior, crashed execution of this exact
            # design attempt. Skip it entirely -- its LLM calls must never
            # be re-issued (ADR-012's no-double-charge requirement is
            # structural precisely because this branch never calls
            # _orchestrate_design_and_review).
            spec = resume_spec
            rationale = resume_rationale or ""
            design_context = resume_design_context
        else:
            design_phase = self._orchestrate_design_and_review(
                prior_records=prior_records,
                signal_briefs=signal_briefs,
                directives=directives,
                exclude_asset_classes=exclude_asset_classes,
                config=config,
                all_gate_results=all_gate_results,
                emit=emit,
                design_attempt=design_attempt,
                phase_back_count=phase_back_count,
                drift_collector=drift_collector,
                regime_summary=regime_summary,
            )
            if design_phase.record is not None:
                return design_phase.record
            spec = design_phase.spec
            rationale = design_phase.rationale
            design_context = design_phase.design_context
            if checkpoint_hook is not None:
                checkpoint_hook(
                    "design_synthesis_boundary",
                    {"spec": spec, "rationale": rationale, "design_context": design_context},
                )

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
        # Budget-exhaustion during this phase (refinement, alignment-fix, or
        # zero-trade repair) is handled internally by
        # ``_orchestrate_refinement_and_alignment`` — mirroring how
        # ``_orchestrate_design_and_review`` handles its own budget
        # short-circuit — so a ``DesignBudgetExhausted`` trip there returns
        # via ``record`` rather than propagating here.
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
        refinement_stalled = synthesis.refinement_stalled
        open_position_entry_reasons = synthesis.open_position_entry_reasons
        ran_on_non_conforming_code = alignment_outcome.ran_on_non_conforming_code
        alignment_rounds = alignment_outcome.alignment_rounds
        trades_aligned = alignment_outcome.trades_aligned
        alignment_reports = alignment_outcome.alignment_reports

        # ── Phases 2.6–3: TRIAL COUNTING → VERIFICATION → ANALYSIS ─────
        pre_verification_state = _DesignAttemptState(
            spec=spec, code=code, trades=trades, metrics=metrics
        )
        (
            metrics,
            is_winning,
            is_publishable,
            publishability_skip,
            narrative,
        ) = self._orchestrate_verification_and_analysis(
            state=pre_verification_state,
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
            design_attempt=design_attempt,
            emit=emit,
        )

        # ── Phase 4: RECORD ───────────────────────────────────────────
        attempt_state = _DesignAttemptState(spec=spec, code=code, trades=trades, metrics=metrics)
        return self._extract_findings_and_assemble_record(
            state=attempt_state,
            config=config,
            narrative=narrative,
            original_spec=original_spec,
            original_code=original_code,
            rationale=rationale,
            requested_symbols=requested_symbols,
            fetched_symbols=fetched_symbols,
            provider_used=provider_used,
            max_rounds_exhausted=max_rounds_exhausted,
            refinement_stalled=refinement_stalled,
            execution_succeeded=execution_succeeded,
            is_winning=is_winning,
            is_publishable=is_publishable,
            publishability_skip_reason=publishability_skip,
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
        backtest-cache telemetry. A ``DesignBudgetExhausted`` trip anywhere
        in this phase (refinement loop or alignment-fix loop) is caught
        internally and converted to a ``record``-carrying short-circuit
        (status ``"failed: budget_exhausted"``) — mirroring
        ``_orchestrate_design_and_review``'s own budget short-circuit — so it
        does NOT propagate to the caller.
        Raises: ``SpecImplementabilityError`` (from the refinement loop) with
        this attempt's ``design_context`` attached when the raiser left it unset.
        """
        try:
            # ── Phase 1b: PRE-SYNTHESIS SPEC GATING (#547 item 1) ─────
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

            # ── Phase 2: CODE REFINEMENT LOOP ─────────────────────────
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
                # attempt's design-loop telemetry to the exception (mirroring
                # the ``drift_collector`` hand-off) so the outer
                # re-entry-exhaustion short-circuit in ``run_cycle`` persists
                # the generation-funnel telemetry of the design loop that
                # actually ran, rather than an empty default. Only set when a
                # raiser didn't already supply one.
                if exc.design_context is None:
                    exc.design_context = design_context
                raise
            # Synthesis fields needed locally for the phase boundary +
            # alignment call; the caller reads the remaining fields off the
            # returned bundle.
            spec = synthesis.spec
            code = synthesis.code
            trades = synthesis.trades
            metrics = synthesis.metrics
            market_data = synthesis.market_data
            execution_succeeded = synthesis.execution_succeeded

            # ═══ Phase 3 → 4 transition: CODE_SYNTHESIS → ═════════════
            # ═══                         BACKTEST_AND_VERIFICATION ════
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

            # ── Phase 2.5: TRADE ALIGNMENT LOOP ────────────────────────
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
        except DesignBudgetExhausted as exc:
            # The per-cycle LLM-call budget (bound for the whole design
            # attempt, not just the design phase — see ``use_budget`` at
            # ``run_cycle``) can trip during refinement, alignment-fix, or
            # zero-trade repair. Those call sites attach the latest
            # spec/code they were working on before propagating; fall back
            # to this attempt's pre-refinement spec/code only if none did
            # (e.g. the trip happened before any leaf call site ran).
            latest_spec = getattr(exc, "latest_spec", spec)
            latest_code = getattr(exc, "latest_code", code)
            abort_reason = (
                f"Refinement/alignment phase exhausted its LLM-call budget "
                f"({exc.calls_made}/{exc.limit} calls) after "
                f"{len(refinement_attempts)} refinement attempt(s)"
            )
            emit("coding", {"sub_phase": "aborted", "reason": abort_reason})
            return _RefinementAlignmentResult(
                record=self._build_short_circuit_record(
                    spec=latest_spec,
                    config=config,
                    code=latest_code,
                    original_spec=original_spec,
                    original_code=original_code,
                    rationale=rationale,
                    all_gate_results=all_gate_results,
                    refinement_attempts=refinement_attempts,
                    short_circuit_status="failed: budget_exhausted",
                    short_circuit_reason=abort_reason,
                    emit=emit,
                    design_context=design_context,
                    phase_back_count=phase_back_count,
                    drift_collector=drift_collector,
                )
            )

    def _orchestrate_verification_and_analysis(
        self,
        *,
        state: _DesignAttemptState,
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
        design_attempt: int,
        emit: PhaseCallback,
    ) -> Tuple[BacktestResult, bool, bool, Optional[str], str]:
        """Count the trial, run verification, and generate the analysis.

        Pre: the refinement + alignment loops have settled the run state;
        ``state`` carries the settled ``spec``/``code``/``trades``/``metrics``
        for this design attempt (``state.code`` is unused here — verification
        and analysis never touch the strategy source).
        Post: returns ``(metrics, is_winning, is_publishable,
        publishability_skip_reason, narrative)``. Increments the
        convergence trial counter (one per refinement round, plus the first),
        runs ``_run_verification_phase`` (which mutates ``metrics`` /
        ``all_gate_results`` and resolves ``is_winning`` / ``is_publishable``),
        and produces the analysis narrative off the conformance-resolved
        alignment report. Emits the terminal
        ``BACKTEST_AND_VERIFICATION → ∅`` phase-transition boundary
        (mirroring the sibling phase methods, each of which owns emitting its
        own exit transition) before returning.
        """
        # ── Phase 2.6: TRIAL COUNTING (issue #247) ────────────────────
        # Every refinement round on the same window contributes to the
        # multiple-testing burden the Deflated Sharpe Ratio corrects for.
        # Increment by ``len(refinement_attempts) + 1`` so the first
        # round (which has no recorded "attempt") still counts.
        self.convergence_tracker.increment_trials(max(1, len(refinement_attempts) + 1))

        # ── Phase 2.7: WALK-FORWARD + ACCEPTANCE + CONFORMANCE + is_winning ────
        verification = self._run_verification_phase(
            spec=state.spec,
            trades=state.trades,
            metrics=state.metrics,
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
        is_publishable = verification.is_publishable
        publishability_skip = verification.publishability_skip_reason
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
            spec=state.spec,
            metrics=metrics,
            trades=state.trades,
            rationale=rationale,
            is_winning=is_winning,
            execution_succeeded=execution_succeeded,
            refinement_attempts=refinement_attempts,
            all_gate_results=all_gate_results,
            alignment_report=latest_alignment_report,
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
            spec=state.spec,
            code=state.code,
            attempt=design_attempt,
        )
        return metrics, is_winning, is_publishable, publishability_skip, narrative

    def _extract_findings_and_assemble_record(
        self,
        *,
        state: _DesignAttemptState,
        config: BacktestConfig,
        narrative: str,
        original_spec: StrategySpec,
        original_code: str,
        rationale: str,
        requested_symbols: List[str],
        fetched_symbols: List[str],
        provider_used: Dict[str, str],
        max_rounds_exhausted: bool,
        refinement_stalled: bool,
        execution_succeeded: bool,
        is_winning: bool,
        is_publishable: bool,
        publishability_skip_reason: Optional[str],  # noqa: F811 — param, not the imported function
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

        Pre: all phases have completed; ``state`` carries the settled
        ``spec``/``code``/``trades``/``metrics`` for this design attempt;
        ``alignment_reports`` holds one report per alignment iteration
        (empty when the loop never ran).
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
            spec=state.spec,
            code=state.code,
            config=config,
            metrics=state.metrics,
            trades=state.trades,
            narrative=narrative,
            original_spec=original_spec,
            original_code=original_code,
            rationale=rationale,
            requested_symbols=requested_symbols,
            fetched_symbols=fetched_symbols,
            provider_used=provider_used,
            max_rounds_exhausted=max_rounds_exhausted,
            refinement_stalled=refinement_stalled,
            execution_succeeded=execution_succeeded,
            is_winning=is_winning,
            is_publishable=is_publishable,
            publishability_skip_reason=publishability_skip_reason,
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

    def _orchestrate_design_and_review(
        self,
        *,
        prior_records: List[StrategyLabRecord],
        signal_briefs: Optional[Dict[str, SignalIntelligenceBriefV1]],
        directives: List[str],
        exclude_asset_classes: Optional[List[str]],
        config: BacktestConfig,
        all_gate_results: List[QualityGateResult],
        emit: PhaseCallback,
        design_attempt: int,
        phase_back_count: int,
        drift_collector: _DriftCollector,
        regime_summary: Optional[RegimeSummary] = None,
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
            signal_briefs=signal_briefs,
            directives=directives,
            exclude_asset_classes=exclude_asset_classes,
            config=config,
            all_gate_results=all_gate_results,
            emit=emit,
            design_attempt=design_attempt,
            drift_collector=drift_collector,
            regime_summary=regime_summary,
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
        if not design_outcome.ready:
            raise OrchestratorContractError(
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
