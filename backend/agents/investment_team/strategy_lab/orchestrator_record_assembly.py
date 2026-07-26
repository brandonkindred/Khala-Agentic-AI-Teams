"""RecordAssemblyMixin — the record-assembly cluster extracted from
:mod:`orchestrator`.

Pure move: every method below is relocated verbatim from ``orchestrator.py``.
No behavior changes. ``RecordAssemblyMixin`` is mixed into
``StrategyLabOrchestrator`` (see the class statement in ``orchestrator.py``
for the current base order); its methods expect the attribute
``StrategyLabOrchestrator.__init__`` sets on ``self``
(``self.convergence_tracker``) — set on the base class and resolved via MRO
on the final composed instance.

This module must not import anything from ``orchestrator.py`` at module level
(that would be circular: ``orchestrator.py`` imports ``RecordAssemblyMixin``
from here before its own class statement executes). Pure helpers shared by
both this cluster and code that stays in ``orchestrator.py`` live in
``_orchestrator_helpers.py`` instead. ``_finalize_loop_telemetry`` is private
to this cluster (used only by the two methods below) and lives here rather
than in ``_orchestrator_helpers.py``; ``orchestrator.py`` re-exports it at
the bottom of the file so ``from investment_team.strategy_lab.orchestrator
import _finalize_loop_telemetry`` keeps working for existing call sites.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from ..models import (
    BacktestConfig,
    BacktestRecord,
    BacktestResult,
    DataProvenance,
    GateEvent,
    StrategyLabRecord,
    StrategySpec,
    TradeRecord,
)
from ..trade_simulator import compute_metrics
from ._orchestrator_helpers import (
    _build_rule_implementation_map,
    _DesignPersistContext,
    _DriftCollector,
)
from .alignment_findings import AlignmentFinding
from .quality_gates.models import QualityGateResult

PhaseCallback = Callable[[str, Dict[str, Any]], None]


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


class RecordAssemblyMixin:
    """Record-assembly cluster mixed into ``StrategyLabOrchestrator``."""

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
        is_publishable: bool = False,
        publishability_skip_reason: Optional[str] = None,  # noqa: F811 — param, not the imported function
        refinement_stalled: bool = False,
    ) -> StrategyLabRecord:
        """Build the final ``StrategyLabRecord`` from a settled cycle.

        Pre: ``spec`` / ``code`` / ``metrics`` / ``trades`` are the
        known-good post-verification state. ``narrative`` came from the
        analysis phase (or a synthetic auto-summary on failure).
        Post: a ``BacktestRecord`` + ``StrategyLabRecord`` are constructed;
        the convergence tracker is updated; a ``"complete"`` event is
        emitted; the record is returned.

        ``status`` resolution mirrors the four terminal-state branches:
          * refinement-loop stall → ``"failed: refinement_stalled"``
          * cap exhausted → ``"failed: max_refinement_rounds"``
          * clean exit → ``"completed"``
          * everything else → ``"failed"``
        """
        now_iso = datetime.now(timezone.utc).isoformat()

        # Cap-exhaustion status: the evaluation-phase site sets
        # ``execution_succeeded=True`` ("anomalous but code is correct"),
        # so without this branch those cycles would silently report
        # ``status="completed"`` despite never reaching a clean backtest.
        # ``refinement_stalled`` is checked first — it can only be True when
        # ``max_rounds_exhausted`` is also True (a stall is a form of
        # exhaustion), and reports the more specific, actionable reason.
        if refinement_stalled:
            backtest_status = "failed: refinement_stalled"
        elif max_rounds_exhausted:
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
            is_publishable=is_publishable,
            publishability_skip_reason=publishability_skip_reason,
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
                "is_publishable": is_publishable,
                "metrics": metrics.model_dump(),
                "refinement_rounds": refinement_rounds,
                "alignment_rounds": alignment_rounds,
                "trades_aligned": trades_aligned,
                "phase_back_count": phase_back_count,
            },
        )

        return record

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
                "is_publishable": False,
                "metrics": backtest_record.result.model_dump(),
                "refinement_rounds": len(refinement_attempts),
                "short_circuit": short_circuit_status,
                "phase_back_count": phase_back_count,
            },
        )

        return record
