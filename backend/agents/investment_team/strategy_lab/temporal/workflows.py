"""Temporal workflow for one Strategy Lab cycle.

``StrategyLabCycleWorkflow`` ports ``StrategyLabOrchestrator.run_cycle`` +
``_run_design_attempt`` (backend/agents/investment_team/strategy_lab/orchestrator.py)
into real async workflow code: the design-reentry retry loop wraps four
phases (design+review, code synthesis, refinement+synthesis+alignment,
verification+analysis), each side effect now an
``await workflow.execute_activity(...)`` call into ``activities.py`` instead
of a direct agent/service call, and every loop bound / mutable-state site the
orchestrator carries on ``self`` (LLM-call budget, backtest cache,
consecutive-spec-mutation counter, drift collector) is workflow-local state
instead.

Design decisions (see the accompanying plan file's Stage 3 section for the
full rationale):
  * Deterministic quality gates (``SpecReadinessGate``, ``StrategySpecValidator``,
    ``CodeSafetyChecker``, ``CodeConformanceGate``, ``PredicateConformanceGate``,
    ``TargetSymbolCoverageGate``) and pure helpers (``repair_spec``,
    ``select_code_path``, ``compile_strategy``, ``inject_universe_and_guard``,
    ``compute_metrics``) are called directly — verified against the installed
    temporalio SDK's actual sandbox restrictions (package imports and file
    reads at import time are unrestricted; only ``os.*``/``datetime.now``/
    ``uuid.uuid4`` are restricted, and only at workflow *runtime*, not import).
  * Every LLM call, backtest execution, and market-data fetch routes through
    an activity in ``activities.py``.
  * The alignment audit (``_run_alignment_audit``, whose near-miss
    adjudication is a synchronous LLM callback bound inside a deterministic
    gate check) and the verification+analysis phase (a single linear pass
    with no bounded retry loop) are each wrapped as one composite activity
    rather than decomposed further — see ``activities.py``'s docstrings.
  * ``os.environ`` reads are hoisted into ``resolve_workflow_config_activity``,
    called once at the top of ``run()`` when ``cycle_input`` doesn't already
    carry resolved config (the batch workflow will normally resolve this once
    per batch and pass it down).
  * ``workflow.uuid4()`` / ``workflow.now()`` replace ``uuid.uuid4()`` /
    ``datetime.now()`` at the handful of call sites that need them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from temporalio import workflow
from temporalio.common import RetryPolicy

from investment_team.models import BacktestConfig, StrategySpec
from investment_team.strategy_lab._orchestrator_helpers import _merge_risk_limits_tighten_only
from investment_team.strategy_lab.exceptions import SpecImplementabilityError
from investment_team.strategy_lab.mechanical_repair import repair_spec, select_code_path
from investment_team.strategy_lab.orchestrator import (
    _REFINEMENT_ALLOWED_KEYS,
    _REFINEMENT_PASSTHROUGH_KEYS,
    _SPEC_MUTATION_TRIP_THRESHOLD,
    MAX_ALIGNMENT_ROUNDS,
    MAX_CODE_REFINEMENT_ROUNDS,
    MAX_DESIGN_REENTRIES,
    _critique_from_readiness,
    _round_demoted_conformance,
    _spec_readiness_signature,
    build_spec_from_dict,
)
from investment_team.strategy_lab.quality_gates.code_conformance import CodeConformanceGate
from investment_team.strategy_lab.quality_gates.code_safety import CodeSafetyChecker
from investment_team.strategy_lab.quality_gates.models import QualityGateResult
from investment_team.strategy_lab.quality_gates.predicate_conformance import (
    PredicateConformanceGate,
    _code_conformance_retries,
)
from investment_team.strategy_lab.quality_gates.spec_readiness import SpecReadinessGate
from investment_team.strategy_lab.quality_gates.strategy_validator import StrategySpecValidator
from investment_team.strategy_lab.quality_gates.target_symbol_coverage import (
    TargetSymbolCoverageGate,
)
from investment_team.strategy_lab.quality_gates.universe_injection import inject_universe_and_guard
from investment_team.strategy_lab.synthesis import CompilerError, compile_strategy
from investment_team.strategy_lab.temporal import activities as act
from investment_team.strategy_lab.temporal.dto import (
    convergence_tracker_from_wire,
    convergence_tracker_to_wire,
)
from investment_team.trade_simulator import compute_metrics

# Bounded retry: a genuine worker crash mid-activity gets a couple of extra
# attempts; the in-activity LLM envelope (for LLM-call activities) already
# owns its own retry/backoff, so this is a coarser, cheaper backstop layered
# on top — not a replacement.
_ACTIVITY_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=2,
)
_ACTIVITY_TIMEOUT = timedelta(minutes=10)
# Backtest execution / market-data fetches can legitimately run long on a
# wide symbol universe; give them a wider ceiling than LLM/gate calls.
_LONG_ACTIVITY_TIMEOUT = timedelta(minutes=30)


async def _exec(name: str, /, **kwargs: Any) -> Any:
    """Thin ``workflow.execute_activity`` wrapper keyed by activity function.

    Preconditions:
        ``name`` names a function attribute of ``activities`` module.
    Postconditions:
        Returns the activity's result. Retries per ``_ACTIVITY_RETRY``.
    """
    fn = getattr(act, name)
    timeout = _LONG_ACTIVITY_TIMEOUT if name in _LONG_RUNNING_ACTIVITIES else _ACTIVITY_TIMEOUT
    args = [kwargs["params"]] if _TAKES_SINGLE_DICT.get(name) else list(kwargs.values())
    return await workflow.execute_activity(
        fn,
        args=args,
        start_to_close_timeout=timeout,
        retry_policy=_ACTIVITY_RETRY,
    )


_LONG_RUNNING_ACTIVITIES = frozenset(
    {
        "run_strategy_code_activity",
        "fetch_market_data_activity",
        "run_verification_and_analysis_activity",
    }
)
# Activities whose signature is a single positional dict (composite
# assemble/short-circuit activities) vs. ordinary keyword parameters.
_TAKES_SINGLE_DICT = {
    "assemble_record_activity": True,
    "build_short_circuit_record_activity": True,
}


def _now_iso() -> str:
    """Replay-safe ISO timestamp for workflow-local drift/history records.

    Postconditions: returns ``workflow.now().isoformat()`` — deterministic
    across replay, unlike ``datetime.now()``.
    """
    return workflow.now().isoformat()


@dataclass
class _DriftState:
    """Workflow-local mirror of ``strategy_lab._orchestrator_helpers._DriftCollector``.

    Plain dict-of-lists so it round-trips through activity JSON payloads
    (``assemble_record_activity``/``build_short_circuit_record_activity``)
    without needing the Pydantic ``SpecRevision``/``CodeRevision``/``GateEvent``
    classes in workflow code.
    """

    spec_history: List[dict] = field(default_factory=list)
    code_history: List[dict] = field(default_factory=list)
    gate_timeline: List[dict] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "spec_history": list(self.spec_history),
            "code_history": list(self.code_history),
            "gate_timeline": list(self.gate_timeline),
        }

    def record_spec_change(
        self,
        *,
        phase: str,
        agent: str,
        before_spec: StrategySpec,
        after_spec: StrategySpec,
        reason: str,
    ) -> None:
        """No-op when the spec hash is unchanged; mirrors the orchestrator's collector."""
        from investment_team.strategy_lab.phases import hash_spec

        before_hash = hash_spec(before_spec)
        after_hash = hash_spec(after_spec)
        if before_hash == after_hash:
            return
        self.spec_history.append(
            {
                "phase": phase,
                "agent": agent,
                "timestamp": _now_iso(),
                "before_hash": before_hash,
                "after_hash": after_hash,
                "diff": "",
                "reason": reason,
                "gate_failures": [],
            }
        )

    def record_code_change(
        self, *, phase: str, agent: str, before_code: str, after_code: str, reason: str
    ) -> None:
        from investment_team.strategy_lab.phases import hash_code

        before_hash = hash_code(before_code)
        after_hash = hash_code(after_code)
        if before_hash == after_hash:
            return
        self.code_history.append(
            {
                "phase": phase,
                "agent": agent,
                "timestamp": _now_iso(),
                "before_hash": before_hash,
                "after_hash": after_hash,
                "diff": "",
                "reason": reason,
                "gate_failures": [],
            }
        )


def _record_gates(
    results: List[QualityGateResult],
    all_gate_results: List[dict],
    *,
    refinement_round: Optional[int] = None,
    gate_name_prefix: str = "",
) -> List[QualityGateResult]:
    """Plain-Python port of ``StrategyLabOrchestrator.record_gates`` (no ``self`` use)."""
    for g in results:
        if refinement_round is not None:
            g.refinement_round = refinement_round
        if gate_name_prefix:
            g.gate_name = f"{gate_name_prefix}{g.gate_name}"
    all_gate_results.extend(g.model_dump(mode="json") for g in results)
    return results


def _build_orchestrator_gate(
    name: str, *, phase: str, severity: str = "critical", details: str, refinement_round: int = 0
) -> QualityGateResult:
    """Plain-Python port of ``StrategyLabOrchestrator.build_orchestrator_gate``."""
    return QualityGateResult(
        gate_name=name,
        phase=phase,
        passed=severity == "info",
        severity=severity,
        details=details,
        refinement_round=refinement_round,
    )


@workflow.defn(name="StrategyLabCycleWorkflow")
class StrategyLabCycleWorkflow:
    """Durable per-cycle Strategy Lab workflow.

    Preconditions:
        ``cycle_input`` (the sole ``run()`` argument) is a JSON-shaped dict:
        ``prior_records`` (list of ``StrategyLabRecord`` dumps),
        ``config`` (``BacktestConfig`` dump), ``signal_brief`` (dump or
        ``None``), ``exclude_asset_classes`` (list or ``None``),
        ``convergence_tracker_state`` (``dto`` wire dict),
        ``workflow_config`` (optional — the ``resolve_workflow_config_activity``
        result; resolved via an activity call when absent).
    Postconditions:
        Returns ``{"record": StrategyLabRecord dump, "convergence_tracker_state": ...}``.
    """

    @workflow.run
    async def run(self, cycle_input: Dict[str, Any]) -> Dict[str, Any]:
        prior_records = cycle_input["prior_records"]
        config_dict = cycle_input["config"]
        signal_brief = cycle_input.get("signal_brief")
        exclude_asset_classes = cycle_input.get("exclude_asset_classes")
        tracker_state = cycle_input.get("convergence_tracker_state") or {}

        wf_config = cycle_input.get("workflow_config")
        if wf_config is None:
            wf_config = await _exec("resolve_workflow_config_activity")

        directives: List[str] = list(cycle_input.get("convergence_directives") or [])

        regime_summary = None
        if wf_config["regime_summary_enabled"]:
            regime_summary = await _exec("compute_regime_summary_activity")

        llm_calls_made = 0
        llm_call_limit = wf_config["design_max_llm_calls"]

        phase_back_count = 0
        drift = _DriftState()
        cumulative_gate_results: List[dict] = []
        last_evidence: Optional[str] = None
        last_spec_dict: Optional[dict] = None
        last_code: str = ""
        last_failure_phase: Optional[str] = None
        last_design_context: Optional[dict] = None

        for design_attempt in range(MAX_DESIGN_REENTRIES + 1):
            attempt_drift = _DriftState()
            backtest_cache: Dict[str, dict] = {}
            consecutive_spec_mutation_rounds: Dict[str, int] = {}
            try:
                record, tracker_state = await self._run_design_attempt(
                    prior_records=prior_records,
                    config_dict=config_dict,
                    signal_brief=signal_brief,
                    exclude_asset_classes=exclude_asset_classes,
                    directives=directives,
                    design_attempt=design_attempt,
                    phase_back_count=phase_back_count,
                    drift=attempt_drift,
                    cumulative_gate_results=cumulative_gate_results,
                    regime_summary=regime_summary,
                    wf_config=wf_config,
                    tracker_state=tracker_state,
                    backtest_cache=backtest_cache,
                    consecutive_spec_mutation_rounds=consecutive_spec_mutation_rounds,
                    llm_calls_made_box=[llm_calls_made],
                    llm_call_limit=llm_call_limit,
                )
                return {"record": record, "convergence_tracker_state": tracker_state}
            except SpecImplementabilityError as exc:
                last_evidence = exc.evidence
                last_spec_dict = (
                    exc.last_spec.model_dump(mode="json") if exc.last_spec is not None else None
                )
                last_code = exc.last_code or ""
                last_failure_phase = exc.failure_phase
                last_design_context = getattr(exc, "_wf_design_context", None)
                phase_back_count += 1
                tracker = convergence_tracker_from_wire(tracker_state)
                tracker.increment_trials(1)
                tracker_state = convergence_tracker_to_wire(tracker)
                drift.spec_history.extend(attempt_drift.spec_history)
                drift.code_history.extend(attempt_drift.code_history)
                drift.gate_timeline.extend(attempt_drift.gate_timeline)
                if design_attempt >= MAX_DESIGN_REENTRIES:
                    break
                directives.append(f"PREVIOUS SPEC UNIMPLEMENTABLE: {exc.evidence}")

        if last_spec_dict is None or last_evidence is None:
            raise RuntimeError(
                "SpecImplementabilityError raised without last_spec/evidence; "
                "cannot build short-circuit record."
            )
        result = await _exec(
            "build_short_circuit_record_activity",
            params={
                "spec": last_spec_dict,
                "config": config_dict,
                "code": last_code,
                "original_spec": last_spec_dict,
                "original_code": last_code,
                "rationale": "",
                "all_gate_results": cumulative_gate_results,
                "refinement_attempts": [],
                "short_circuit_status": "failed: spec_unimplementable",
                "short_circuit_reason": (
                    f"Spec unimplementable after {MAX_DESIGN_REENTRIES + 1} design attempts "
                    f"(last failure_phase={last_failure_phase}): {last_evidence}"
                ),
                "design_context": last_design_context,
                "phase_back_count": phase_back_count,
                "drift_collector": drift.to_dict(),
                "convergence_tracker_state": tracker_state,
            },
        )
        return {
            "record": result["record"],
            "convergence_tracker_state": result["convergence_tracker_state"],
        }

    # ------------------------------------------------------------------
    # Phase 1+2+3+4: one design attempt (ports _run_design_attempt)
    # ------------------------------------------------------------------

    async def _run_design_attempt(
        self,
        *,
        prior_records: List[dict],
        config_dict: dict,
        signal_brief: Optional[dict],
        exclude_asset_classes: Optional[List[str]],
        directives: List[str],
        design_attempt: int,
        phase_back_count: int,
        drift: _DriftState,
        cumulative_gate_results: List[dict],
        regime_summary: Optional[dict],
        wf_config: dict,
        tracker_state: dict,
        backtest_cache: Dict[str, dict],
        consecutive_spec_mutation_rounds: Dict[str, int],
        llm_calls_made_box: List[int],
        llm_call_limit: int,
    ) -> Tuple[dict, dict]:
        """Returns ``(record_dict, updated_tracker_state)``."""
        all_gate_results: List[dict] = list(cumulative_gate_results)

        design_result = await self._orchestrate_design_and_review(
            prior_records=prior_records,
            config_dict=config_dict,
            signal_brief=signal_brief,
            exclude_asset_classes=exclude_asset_classes,
            directives=directives,
            all_gate_results=all_gate_results,
            design_attempt=design_attempt,
            phase_back_count=phase_back_count,
            drift=drift,
            regime_summary=regime_summary,
            wf_config=wf_config,
            tracker_state=tracker_state,
            llm_calls_made_box=llm_calls_made_box,
            llm_call_limit=llm_call_limit,
        )
        if design_result.get("record") is not None:
            return design_result["record"], design_result["tracker_state"]
        spec_dict = design_result["spec"]
        rationale = design_result["rationale"]
        design_context = design_result["design_context"]

        synth_result = await self._synthesize_initial_code(
            spec_dict=spec_dict,
            config_dict=config_dict,
            rationale=rationale,
            all_gate_results=all_gate_results,
            design_attempt=design_attempt,
            phase_back_count=phase_back_count,
            drift=drift,
            design_context=design_context,
            tracker_state=tracker_state,
        )
        if synth_result.get("record") is not None:
            return synth_result["record"], synth_result["tracker_state"]
        code = synth_result["code"]
        original_spec_dict = synth_result["original_spec"]
        original_code = synth_result["original_code"]
        config_dict = synth_result["config"]

        refine_align_result = await self._orchestrate_refinement_and_alignment(
            spec_dict=spec_dict,
            code=code,
            config_dict=config_dict,
            original_spec_dict=original_spec_dict,
            original_code=original_code,
            rationale=rationale,
            all_gate_results=all_gate_results,
            design_attempt=design_attempt,
            phase_back_count=phase_back_count,
            drift=drift,
            design_context=design_context,
            tracker_state=tracker_state,
            backtest_cache=backtest_cache,
            consecutive_spec_mutation_rounds=consecutive_spec_mutation_rounds,
        )
        if refine_align_result.get("record") is not None:
            return refine_align_result["record"], refine_align_result["tracker_state"]

        synthesis = refine_align_result["synthesis"]
        alignment = refine_align_result["alignment"]

        verification = await _exec(
            "run_verification_and_analysis_activity",
            spec=synthesis["spec"],
            trades=alignment["trades"],
            metrics=alignment["metrics"],
            market_data=synthesis["market_data"],
            config=config_dict,
            execution_succeeded=synthesis["execution_succeeded"],
            trades_aligned=alignment["trades_aligned"],
            alignment_reports=alignment["alignment_reports"],
            all_gate_results=all_gate_results,
            runtime_lookahead_violation=synthesis["runtime_lookahead_violation"],
            open_position_entry_reasons=synthesis["open_position_entry_reasons"],
            refinement_attempts=refine_align_result["refinement_attempts"],
            rationale=rationale,
            convergence_tracker_state=tracker_state,
        )
        all_gate_results = verification["all_gate_results"]
        tracker_state = verification["convergence_tracker_state"]

        alignment_findings = (
            alignment["alignment_reports"][-1].get("alignment_findings", [])
            if alignment["alignment_reports"]
            else []
        )
        assemble_result = await _exec(
            "assemble_record_activity",
            params={
                "spec": alignment["spec"],
                "code": alignment["code"],
                "config": config_dict,
                "metrics": verification["metrics"],
                "trades": alignment["trades"],
                "narrative": verification["narrative"],
                "original_spec": original_spec_dict,
                "original_code": original_code,
                "rationale": rationale,
                "requested_symbols": synthesis["requested_symbols"],
                "fetched_symbols": synthesis["fetched_symbols"],
                "provider_used": synthesis["provider_used"],
                "max_rounds_exhausted": synthesis["max_rounds_exhausted"],
                "execution_succeeded": synthesis["execution_succeeded"],
                "is_winning": verification["is_winning"],
                "trades_aligned": alignment["trades_aligned"],
                "refinement_rounds": len(refine_align_result["refinement_attempts"]),
                "alignment_rounds": len(alignment["alignment_attempts"]),
                "all_gate_results": all_gate_results,
                "ran_on_non_conforming_code": alignment["ran_on_non_conforming_code"],
                "design_context": design_context,
                "alignment_findings": alignment_findings,
                "phase_back_count": phase_back_count,
                "drift_collector": drift.to_dict(),
                "convergence_tracker_state": tracker_state,
            },
        )
        return assemble_result["record"], assemble_result["convergence_tracker_state"]

    # ------------------------------------------------------------------
    # Phase 1: design + review (ports _orchestrate_design_and_review)
    # ------------------------------------------------------------------

    async def _orchestrate_design_and_review(
        self,
        *,
        prior_records: List[dict],
        config_dict: dict,
        signal_brief: Optional[dict],
        exclude_asset_classes: Optional[List[str]],
        directives: List[str],
        all_gate_results: List[dict],
        design_attempt: int,
        phase_back_count: int,
        drift: _DriftState,
        regime_summary: Optional[dict],
        wf_config: dict,
        tracker_state: dict,
        llm_calls_made_box: List[int],
        llm_call_limit: int,
    ) -> Dict[str, Any]:
        design_outcome = await self._run_design_loop(
            prior_records=prior_records,
            signal_brief=signal_brief,
            directives=directives,
            exclude_asset_classes=exclude_asset_classes,
            config_dict=config_dict,
            all_gate_results=all_gate_results,
            drift=drift,
            regime_summary=regime_summary,
            wf_config=wf_config,
            llm_calls_made_box=llm_calls_made_box,
            llm_call_limit=llm_call_limit,
        )
        design_context = {
            "rounds": design_outcome["rounds"],
            "critiques": design_outcome["critique_history"],
            "stop_reason": design_outcome["stop_reason"],
            "loop_telemetry": design_outcome.get("loop_telemetry", {}),
        }
        if not design_outcome["ready"]:
            if design_outcome["budget_exhausted"]:
                short_circuit_status = "failed: budget_exhausted"
                abort_reason = (
                    f"Design phase exhausted its LLM-call budget "
                    f"({llm_calls_made_box[0]}/{llm_call_limit} calls) after "
                    f"{design_context['rounds']} round(s)"
                )
            elif design_outcome["stop_reason"] == "stalled":
                short_circuit_status = "failed: design_stalled"
                abort_reason = (
                    f"Design loop stalled — the open-issue set was unchanged for "
                    f"{wf_config['design_review_stall_rounds']} consecutive round(s) "
                    f"after {design_context['rounds']} round(s)"
                )
            else:
                short_circuit_status = "failed: design_not_ready"
                abort_reason = (
                    f"Design did not reach readiness after {design_context['rounds']} round(s)"
                )

            result = await _exec(
                "build_short_circuit_record_activity",
                params={
                    "spec": design_outcome["spec"],
                    "config": config_dict,
                    "code": "",
                    "original_spec": design_outcome["spec"],
                    "original_code": "",
                    "rationale": design_outcome["rationale"],
                    "all_gate_results": all_gate_results,
                    "refinement_attempts": [],
                    "short_circuit_status": short_circuit_status,
                    "short_circuit_reason": abort_reason,
                    "design_context": design_context,
                    "phase_back_count": phase_back_count,
                    "drift_collector": drift.to_dict(),
                    "convergence_tracker_state": tracker_state,
                },
            )
            return {
                "record": result["record"],
                "tracker_state": result["convergence_tracker_state"],
            }

        return {
            "record": None,
            "spec": design_outcome["spec"],
            "rationale": design_outcome["rationale"],
            "design_context": design_context,
        }

    async def _run_design_loop(
        self,
        *,
        prior_records: List[dict],
        signal_brief: Optional[dict],
        directives: List[str],
        exclude_asset_classes: Optional[List[str]],
        config_dict: dict,
        all_gate_results: List[dict],
        drift: _DriftState,
        regime_summary: Optional[dict],
        wf_config: dict,
        llm_calls_made_box: List[int],
        llm_call_limit: int,
    ) -> Dict[str, Any]:
        try:
            gen = await _exec(
                "design_generate_activity",
                prior_records=prior_records,
                signal_brief=signal_brief,
                convergence_directives=directives or None,
                exclude_asset_classes=exclude_asset_classes,
                regime_summary=regime_summary,
            )
            llm_calls_made_box[0] += 1
        except Exception:
            if llm_calls_made_box[0] >= llm_call_limit:
                return {
                    "spec": None,
                    "rationale": "",
                    "ready": False,
                    "rounds": 0,
                    "critique_history": [],
                    "budget_exhausted": True,
                    "stop_reason": "budget_exhausted",
                    "loop_telemetry": {},
                }
            raise
        strategy_id = f"strat-{workflow.uuid4().hex[:8]}"
        spec_dict = build_spec_from_dict(gen["strategy_dict"], strategy_id=strategy_id).model_dump(
            mode="json"
        )
        rationale = gen["rationale"]

        max_rounds = wf_config["design_review_rounds"]
        stall_rounds = wf_config["design_review_stall_rounds"]
        mechanical_repair_enabled = wf_config["mechanical_repair_enabled"]

        critique_history: List[dict] = []
        open_issue_history: List[List[str]] = []
        ever_resolved: set = set()
        ready = False
        stop_reason = "round_cap"
        last_readiness_signature: Optional[tuple] = None
        readiness_results: List[dict] = []

        for review_round in range(max_rounds):
            spec_obj = StrategySpec.parse_persisted(spec_dict)

            sig = _spec_readiness_signature(spec_obj)
            if sig != last_readiness_signature:
                readiness_results = await self._validate_readiness(spec_dict, config_dict)
                last_readiness_signature = sig
            _record_gates(
                [QualityGateResult.model_validate(g) for g in readiness_results],
                all_gate_results,
                refinement_round=-1,
            )
            deterministic_ready = not any(
                not g["passed"] and g["severity"] == "critical" for g in readiness_results
            )

            if mechanical_repair_enabled:
                (
                    spec_dict,
                    readiness_results,
                    last_readiness_signature,
                    deterministic_ready,
                ) = await self._run_mechanical_repair_stage(
                    spec_dict=spec_dict,
                    config_dict=config_dict,
                    readiness_results=readiness_results,
                    last_readiness_signature=last_readiness_signature,
                    deterministic_ready=deterministic_ready,
                    all_gate_results=all_gate_results,
                    drift=drift,
                )

            if deterministic_ready:
                try:
                    critique = await _exec(
                        "design_review_activity",
                        spec=spec_dict,
                        readiness_results=readiness_results,
                        prior_critiques=critique_history,
                    )
                    llm_calls_made_box[0] += 1
                except Exception:
                    if llm_calls_made_box[0] >= llm_call_limit:
                        return {
                            "spec": spec_dict,
                            "rationale": rationale,
                            "ready": False,
                            "rounds": len(critique_history),
                            "critique_history": critique_history,
                            "budget_exhausted": True,
                            "stop_reason": "budget_exhausted",
                            "loop_telemetry": {},
                        }
                    raise
            else:
                critique = _critique_from_readiness(
                    [QualityGateResult.model_validate(g) for g in readiness_results]
                ).model_dump(mode="json")

            critique_history.append(critique)
            open_ids = sorted(
                {
                    i["issue_id"]
                    for i in critique["issues"]
                    if i["severity"] in ("warning", "critical")
                }
            )
            resolved = set(open_issue_history[-1]) - set(open_ids) if open_issue_history else set()
            ever_resolved |= resolved
            regressed = set(open_ids) & ever_resolved - (
                set(open_issue_history[-1]) if open_issue_history else set()
            )
            open_issue_history.append(open_ids)

            if critique["ready"]:
                ready = True
                stop_reason = "ready"
                break

            if review_round < max_rounds - 1 and len(open_issue_history) >= stall_rounds:
                window = open_issue_history[-stall_rounds:]
                if window[0] and all(set(w) == set(window[0]) for w in window):
                    stop_reason = "stalled"
                    break

            if review_round >= max_rounds - 1:
                stop_reason = "round_cap"
                break

            regression_notice = _format_regression_notice_dict(critique, regressed)
            try:
                rev = await _exec(
                    "design_revise_activity",
                    prior_spec=spec_dict,
                    critique=critique,
                    prior_critiques=critique_history,
                    regression_notice=regression_notice,
                )
                llm_calls_made_box[0] += 1
            except Exception:
                if llm_calls_made_box[0] >= llm_call_limit:
                    return {
                        "spec": spec_dict,
                        "rationale": rationale,
                        "ready": False,
                        "rounds": len(critique_history),
                        "critique_history": critique_history,
                        "budget_exhausted": True,
                        "stop_reason": "budget_exhausted",
                        "loop_telemetry": {},
                    }
                raise
            prev_spec_dict = spec_dict
            strategy_id_after = StrategySpec.parse_persisted(spec_dict).strategy_id
            spec_dict = build_spec_from_dict(
                rev["strategy_dict"], strategy_id=strategy_id_after
            ).model_dump(mode="json")
            rationale = rev["rationale"]
            drift.record_spec_change(
                phase="design_review",
                agent="DesignAgent",
                before_spec=StrategySpec.parse_persisted(prev_spec_dict),
                after_spec=StrategySpec.parse_persisted(spec_dict),
                reason=critique.get("rationale", ""),
            )

        return {
            "spec": spec_dict,
            "rationale": rationale,
            "ready": ready,
            "rounds": len(critique_history),
            "critique_history": critique_history,
            "budget_exhausted": False,
            "stop_reason": stop_reason,
            "loop_telemetry": {},
        }

    async def _validate_readiness(self, spec_dict: dict, config_dict: dict) -> List[dict]:
        spec_obj = StrategySpec.parse_persisted(spec_dict)
        config_obj = BacktestConfig(**config_dict)
        prices = await _exec(
            "resolve_readiness_prices_activity",
            symbols=await _exec("resolve_symbols_activity", spec=spec_dict),
            asset_class=spec_obj.asset_class,
        )

        def _price_provider(symbol: str, asset_class: str) -> float:
            return prices.get(symbol, float("nan"))

        gate = SpecReadinessGate(market_sample_provider=_price_provider, backtest_config=config_obj)
        results = gate.validate(spec_obj, phase="design", backtest_config=config_obj)
        return [g.model_dump(mode="json") for g in results]

    async def _run_mechanical_repair_stage(
        self,
        *,
        spec_dict: dict,
        config_dict: dict,
        readiness_results: List[dict],
        last_readiness_signature: Optional[tuple],
        deterministic_ready: bool,
        all_gate_results: List[dict],
        drift: _DriftState,
    ) -> Tuple[dict, List[dict], Optional[tuple], bool]:
        spec_obj = StrategySpec.parse_persisted(spec_dict)
        config_obj = BacktestConfig(**config_dict)
        pre_repair_spec = spec_obj

        outcome = repair_spec(spec_obj, config=config_obj)
        if outcome.repair_actions:
            spec_obj = outcome.spec
            readiness_results = await self._validate_readiness(
                spec_obj.model_dump(mode="json"), config_dict
            )
            last_readiness_signature = _spec_readiness_signature(spec_obj)
            deterministic_ready = not any(
                not g["passed"] and g["severity"] == "critical" for g in readiness_results
            )

        if deterministic_ready:
            compile_action = select_code_path(spec_obj)
            if compile_action.requires_custom_code != spec_obj.requires_custom_code:
                spec_obj = spec_obj.model_copy(
                    update={"requires_custom_code": compile_action.requires_custom_code}
                )
                readiness_results = await self._validate_readiness(
                    spec_obj.model_dump(mode="json"), config_dict
                )
                last_readiness_signature = _spec_readiness_signature(spec_obj)
                deterministic_ready = not any(
                    not g["passed"] and g["severity"] == "critical" for g in readiness_results
                )

        drift.record_spec_change(
            phase="design",
            agent="MechanicalRepair",
            before_spec=pre_repair_spec,
            after_spec=spec_obj,
            reason="deterministic mechanical auto-repair",
        )
        return (
            spec_obj.model_dump(mode="json"),
            readiness_results,
            last_readiness_signature,
            deterministic_ready,
        )

    # ------------------------------------------------------------------
    # Phase 2: code synthesis (ports _synthesize_initial_code)
    # ------------------------------------------------------------------

    async def _synthesize_initial_code(
        self,
        *,
        spec_dict: dict,
        config_dict: dict,
        rationale: str,
        all_gate_results: List[dict],
        design_attempt: int,
        phase_back_count: int,
        drift: _DriftState,
        design_context: dict,
        tracker_state: dict,
    ) -> Dict[str, Any]:
        spec_obj = StrategySpec.parse_persisted(spec_dict)
        code = ""
        if not spec_obj.requires_custom_code:
            try:
                code = compile_strategy(spec_obj)
            except CompilerError:
                spec_obj = spec_obj.model_copy(update={"requires_custom_code": True})

        if not code:
            try:
                synth = await _exec(
                    "code_synthesis_activity", spec=spec_obj.model_dump(mode="json")
                )
                code = synth["code"]
            except Exception as exc:  # noqa: BLE001 — CodeSynthesisError-shaped ApplicationError
                result = await _exec(
                    "build_short_circuit_record_activity",
                    params={
                        "spec": spec_obj.model_dump(mode="json"),
                        "config": config_dict,
                        "code": "",
                        "original_spec": spec_obj.model_dump(mode="json"),
                        "original_code": "",
                        "rationale": rationale,
                        "all_gate_results": all_gate_results,
                        "refinement_attempts": [],
                        "short_circuit_status": "failed: code_synthesis",
                        "short_circuit_reason": f"Code synthesis failed: {exc}",
                        "design_context": design_context,
                        "phase_back_count": phase_back_count,
                        "drift_collector": drift.to_dict(),
                        "convergence_tracker_state": tracker_state,
                    },
                )
                return {
                    "record": result["record"],
                    "tracker_state": result["convergence_tracker_state"],
                }

        drift.record_code_change(
            phase="synthesis",
            agent="compiler" if not spec_obj.requires_custom_code else "CodeSynthesisAgent",
            before_code="",
            after_code=code,
            reason="initial code synthesis",
        )
        spec_obj = spec_obj.model_copy(update={"strategy_code": code})
        original_spec = spec_obj.model_copy(deep=True)

        fee_defaults = {}
        config_obj = BacktestConfig(**config_dict)
        if config_obj.transaction_cost_bps == 5.0 and config_obj.slippage_bps == 2.0:
            from investment_team.models import get_fee_defaults

            fee_defaults = get_fee_defaults(spec_obj.asset_class)
        if fee_defaults:
            config_obj = config_obj.model_copy(update=fee_defaults)

        return {
            "record": None,
            "code": code,
            "original_spec": original_spec.model_dump(mode="json"),
            "original_code": code,
            "config": config_obj.model_dump(mode="json"),
        }

    # ------------------------------------------------------------------
    # Phase 3: refinement + alignment
    # ------------------------------------------------------------------

    async def _orchestrate_refinement_and_alignment(
        self,
        *,
        spec_dict: dict,
        code: str,
        config_dict: dict,
        original_spec_dict: dict,
        original_code: str,
        rationale: str,
        all_gate_results: List[dict],
        design_attempt: int,
        phase_back_count: int,
        drift: _DriftState,
        design_context: dict,
        tracker_state: dict,
        backtest_cache: Dict[str, dict],
        consecutive_spec_mutation_rounds: Dict[str, int],
    ) -> Dict[str, Any]:
        pre_synth_record = await self._run_pre_synthesis_phase(
            spec_dict=spec_dict,
            config_dict=config_dict,
            all_gate_results=all_gate_results,
            code=code,
            original_spec_dict=original_spec_dict,
            original_code=original_code,
            rationale=rationale,
            drift=drift,
            design_context=design_context,
            phase_back_count=phase_back_count,
            tracker_state=tracker_state,
        )
        if pre_synth_record is not None:
            return pre_synth_record

        refinement_attempts: List[str] = []
        zero_trade_attempts: List[str] = []
        synthesis = await self._run_synthesis_loop(
            spec_dict=spec_dict,
            code=code,
            config_dict=config_dict,
            all_gate_results=all_gate_results,
            refinement_attempts=refinement_attempts,
            zero_trade_attempts=zero_trade_attempts,
            drift=drift,
            backtest_cache=backtest_cache,
            consecutive_spec_mutation_rounds=consecutive_spec_mutation_rounds,
        )

        alignment = await self._run_trade_alignment_loop(
            spec_dict=synthesis["spec"],
            code=synthesis["code"],
            trades=synthesis["trades"],
            metrics=synthesis["metrics"],
            market_data=synthesis["market_data"],
            config_dict=config_dict,
            execution_succeeded=synthesis["execution_succeeded"],
            all_gate_results=all_gate_results,
            ran_on_non_conforming_code=synthesis["ran_on_non_conforming_code"],
            drift=drift,
            backtest_cache=backtest_cache,
        )

        return {
            "record": None,
            "synthesis": synthesis,
            "alignment": alignment,
            "refinement_attempts": refinement_attempts,
            "tracker_state": tracker_state,
        }

    async def _run_pre_synthesis_phase(
        self,
        *,
        spec_dict: dict,
        config_dict: dict,
        all_gate_results: List[dict],
        code: str,
        original_spec_dict: dict,
        original_code: str,
        rationale: str,
        drift: _DriftState,
        design_context: dict,
        phase_back_count: int,
        tracker_state: dict,
    ) -> Optional[Dict[str, Any]]:
        spec_obj = StrategySpec.parse_persisted(spec_dict)
        raw_gates = StrategySpecValidator().validate(spec_obj)
        pre_spec_gates = [
            g
            for g in raw_gates
            if not (g.severity == "critical" and g.details.startswith("strategy_code is missing"))
        ]
        _record_gates(pre_spec_gates, all_gate_results, refinement_round=-1)
        criticals = [g for g in pre_spec_gates if not g.passed and g.severity == "critical"]
        if not criticals:
            return None

        result = await _exec(
            "build_short_circuit_record_activity",
            params={
                "spec": spec_dict,
                "config": config_dict,
                "code": code,
                "original_spec": original_spec_dict,
                "original_code": original_code,
                "rationale": rationale,
                "all_gate_results": all_gate_results,
                "refinement_attempts": [],
                "short_circuit_status": "failed: spec_validation",
                "short_circuit_reason": (
                    "Spec validation failed before code synthesis: "
                    + "; ".join(g.details for g in criticals)
                ),
                "design_context": design_context,
                "phase_back_count": phase_back_count,
                "drift_collector": drift.to_dict(),
                "convergence_tracker_state": tracker_state,
            },
        )
        return {"record": result["record"], "tracker_state": result["convergence_tracker_state"]}

    async def _run_synthesis_loop(
        self,
        *,
        spec_dict: dict,
        code: str,
        config_dict: dict,
        all_gate_results: List[dict],
        refinement_attempts: List[str],
        zero_trade_attempts: List[str],
        drift: _DriftState,
        backtest_cache: Dict[str, dict],
        consecutive_spec_mutation_rounds: Dict[str, int],
    ) -> Dict[str, Any]:
        trades: List[dict] = []
        open_position_entry_reasons: List[str] = []
        metrics = None
        execution_succeeded = False
        market_data: Optional[Dict[str, List[dict]]] = None
        requested_symbols: List[str] = []
        fetched_symbols: List[str] = []
        provider_used: Dict[str, str] = {}
        max_rounds_exhausted = False
        runtime_lookahead_violation = False
        predicate_conformance_attempts = 0
        ran_on_non_conforming_code = False
        exec_result: Optional[dict] = None

        for round_num in range(MAX_CODE_REFINEMENT_ROUNDS):
            spec_obj = StrategySpec.parse_persisted(spec_dict)
            before_inject = code
            code = inject_universe_and_guard(code, spec_obj)
            if code != before_inject:
                spec_obj = spec_obj.model_copy(update={"strategy_code": code})
                spec_dict = spec_obj.model_dump(mode="json")
                drift.record_code_change(
                    phase="synthesis",
                    agent="universe_injector",
                    before_code=before_inject,
                    after_code=code,
                    reason="deterministic UNIVERSE + symbol-guard injection",
                )

            round_gate_results, predicate_conformance_attempts = (
                self._run_synthesis_validation_gates(
                    spec_obj=spec_obj,
                    code=code,
                    round_num=round_num,
                    predicate_conformance_attempts=predicate_conformance_attempts,
                    all_gate_results=all_gate_results,
                )
            )
            critical_failures = [
                g for g in round_gate_results if not g.passed and g.severity == "critical"
            ]
            if critical_failures:
                failure_details = "\n".join(
                    f"- [{g.gate_name}{(':' + g.rule_id) if g.rule_id else ''}] {g.details}"
                    for g in critical_failures
                )
                spec_dict, code, exhausted = await self._refine_or_exhaust(
                    spec_dict=spec_dict,
                    code=code,
                    failure_phase="validation",
                    failure_details=failure_details,
                    metrics=None,
                    refinement_attempts=refinement_attempts,
                    round_num=round_num,
                    default_change_label="validation fix",
                    drift=drift,
                    consecutive_spec_mutation_rounds=consecutive_spec_mutation_rounds,
                )
                if exhausted:
                    max_rounds_exhausted = True
                    break
                continue

            if market_data is None:
                symbols = await _exec("resolve_symbols_activity", spec=spec_dict)
                requested_symbols = list(symbols)
                if not symbols:
                    all_gate_results.append(
                        _build_orchestrator_gate(
                            "market_data",
                            phase="synthesis",
                            details=f"No market data available for asset class '{spec_obj.asset_class}'.",
                            refinement_round=round_num,
                        ).model_dump(mode="json")
                    )
                    break
                fetched = await _exec(
                    "fetch_market_data_activity",
                    symbols=symbols,
                    asset_class=spec_obj.asset_class,
                    start_date=BacktestConfig(**config_dict).start_date,
                    end_date=BacktestConfig(**config_dict).end_date,
                )
                market_data = fetched["data"]
                provider_used = fetched["provider_used"]
                fetched_symbols = list(market_data.keys())
                if not market_data:
                    all_gate_results.append(
                        _build_orchestrator_gate(
                            "market_data",
                            phase="synthesis",
                            details=f"No market data available for asset class '{spec_obj.asset_class}'.",
                            refinement_round=round_num,
                        ).model_dump(mode="json")
                    )
                    break
                fetch_coverage_gates = TargetSymbolCoverageGate().check_fetch(
                    spec_obj, requested_symbols, fetched_symbols
                )
                _record_gates(fetch_coverage_gates, all_gate_results, refinement_round=round_num)
                if any(not g.passed and g.severity == "critical" for g in fetch_coverage_gates):
                    break

            cache_key = _backtest_cache_key(code, market_data, config_dict, spec_dict)
            if cache_key in backtest_cache:
                exec_result = backtest_cache[cache_key]
            else:
                exec_result = await _exec(
                    "run_strategy_code_activity",
                    strategy_code=code,
                    market_data=market_data,
                    config=config_dict,
                    strategy=spec_dict,
                )
                backtest_cache[cache_key] = exec_result
            runtime_lookahead_violation = exec_result.get("error_type") == "lookahead_violation"

            if not exec_result["success"]:
                all_gate_results.append(
                    _build_orchestrator_gate(
                        "code_execution",
                        phase="synthesis",
                        details=(
                            f"Execution failed ({exec_result.get('error_type')}): "
                            f"{exec_result.get('stderr', '')}"
                        ),
                        refinement_round=round_num,
                    ).model_dump(mode="json")
                )
                failure_details = f"Error type: {exec_result.get('error_type')}\nstderr:\n{exec_result.get('stderr', '')}"
                spec_dict, code, exhausted = await self._refine_or_exhaust(
                    spec_dict=spec_dict,
                    code=code,
                    failure_phase="execution",
                    failure_details=failure_details,
                    metrics=None,
                    refinement_attempts=refinement_attempts,
                    round_num=round_num,
                    default_change_label="execution fix",
                    drift=drift,
                    consecutive_spec_mutation_rounds=consecutive_spec_mutation_rounds,
                )
                if exhausted:
                    max_rounds_exhausted = True
                    break
                continue

            trades = exec_result["trades"]
            ran_on_non_conforming_code = _round_demoted_conformance(round_gate_results)
            open_position_entry_reasons = exec_result.get("open_position_entry_reasons", [])
            trade_coverage_gates = TargetSymbolCoverageGate().check_trades(
                spec_obj, [_trade_from_dict(t) for t in trades]
            )
            _record_gates(trade_coverage_gates, all_gate_results, refinement_round=round_num)
            if any(not g.passed and g.severity == "critical" for g in trade_coverage_gates):
                max_rounds_exhausted = True
                break

            evaluation = await self._evaluate_synthesis_round(
                spec_dict=spec_dict,
                code=code,
                trades=trades,
                exec_result=exec_result,
                market_data=market_data,
                config_dict=config_dict,
                round_num=round_num,
                all_gate_results=all_gate_results,
                refinement_attempts=refinement_attempts,
                zero_trade_attempts=zero_trade_attempts,
                drift=drift,
            )
            spec_dict, code = evaluation["spec"], evaluation["code"]
            trades, metrics = evaluation["trades"], evaluation["metrics"]
            ran_on_non_conforming_code = evaluation["ran_on_non_conforming_code"]
            exec_result = evaluation["exec_result"]
            runtime_lookahead_violation = evaluation["runtime_lookahead_violation"]
            if evaluation["action"] == "exhausted":
                max_rounds_exhausted = True
                break
            if evaluation["action"] == "continue":
                continue

            execution_succeeded = True
            break

        if metrics is None:
            metrics = compute_metrics(
                [],
                BacktestConfig(**config_dict).initial_capital,
                BacktestConfig(**config_dict).start_date,
                BacktestConfig(**config_dict).end_date,
            ).model_dump(mode="json")

        return {
            "spec": spec_dict,
            "code": code,
            "trades": trades,
            "metrics": metrics,
            "market_data": market_data,
            "requested_symbols": requested_symbols,
            "fetched_symbols": fetched_symbols,
            "execution_succeeded": execution_succeeded,
            "max_rounds_exhausted": max_rounds_exhausted,
            "provider_used": provider_used,
            "open_position_entry_reasons": open_position_entry_reasons,
            "runtime_lookahead_violation": runtime_lookahead_violation,
            "ran_on_non_conforming_code": ran_on_non_conforming_code,
        }

    def _run_synthesis_validation_gates(
        self,
        *,
        spec_obj: StrategySpec,
        code: str,
        round_num: int,
        predicate_conformance_attempts: int,
        all_gate_results: List[dict],
    ) -> Tuple[List[QualityGateResult], int]:
        round_gate_results: List[QualityGateResult] = []
        code_gates = CodeSafetyChecker().check(code, spec_obj)
        round_gate_results.extend(code_gates)
        conformance_gates = CodeConformanceGate().check(code, spec_obj)
        round_gate_results.extend(conformance_gates)
        if not any(not g.passed and g.severity == "critical" for g in round_gate_results):
            pred_conf_gates = PredicateConformanceGate().check(
                code, spec_obj, attempt=predicate_conformance_attempts
            )
            round_gate_results.extend(pred_conf_gates)
            if any(not g.passed and g.severity == "critical" for g in pred_conf_gates):
                predicate_conformance_attempts += 1
        _record_gates(round_gate_results, all_gate_results, refinement_round=round_num)
        return round_gate_results, predicate_conformance_attempts

    async def _evaluate_synthesis_round(
        self,
        *,
        spec_dict: dict,
        code: str,
        trades: List[dict],
        exec_result: dict,
        market_data: Dict[str, List[dict]],
        config_dict: dict,
        round_num: int,
        all_gate_results: List[dict],
        refinement_attempts: List[str],
        zero_trade_attempts: List[str],
        drift: _DriftState,
    ) -> Dict[str, Any]:
        config_obj = BacktestConfig(**config_dict)
        metrics_obj = compute_metrics(
            [_trade_from_dict(t) for t in trades],
            config_obj.initial_capital,
            config_obj.start_date,
            config_obj.end_date,
        )
        if exec_result.get("execution_diagnostics") is not None:
            from investment_team.models import BacktestExecutionDiagnostics

            metrics_obj = metrics_obj.model_copy(
                update={
                    "execution_diagnostics": BacktestExecutionDiagnostics(
                        **exec_result["execution_diagnostics"]
                    )
                }
            )

        anomaly_gates = await self._run_anomaly_check(metrics_obj, trades, config_obj, market_data)
        _record_gates(anomaly_gates, all_gate_results, refinement_round=round_num)
        critical_anomalies = [g for g in anomaly_gates if not g.passed and g.severity == "critical"]

        if not critical_anomalies:
            return {
                "action": "success",
                "spec": spec_dict,
                "code": code,
                "trades": trades,
                "metrics": metrics_obj.model_dump(mode="json"),
                "exec_result": exec_result,
                "ran_on_non_conforming_code": _round_demoted_conformance([]),
                "runtime_lookahead_violation": exec_result.get("error_type")
                == "lookahead_violation",
            }

        recovery = await self._handle_critical_anomalies(
            spec_dict=spec_dict,
            code=code,
            trades=trades,
            metrics=metrics_obj.model_dump(mode="json"),
            exec_result=exec_result,
            market_data=market_data,
            config_dict=config_dict,
            critical_anomalies=critical_anomalies,
            all_gate_results=all_gate_results,
            refinement_attempts=refinement_attempts,
            zero_trade_attempts=zero_trade_attempts,
            round_num=round_num,
            drift=drift,
        )
        return {
            "action": "exhausted" if recovery["exhausted"] else "continue",
            "spec": recovery["spec"],
            "code": recovery["code"],
            "trades": recovery["trades"],
            "metrics": recovery["metrics"],
            "exec_result": recovery["exec_result"],
            "ran_on_non_conforming_code": recovery["ran_on_non_conforming_code"],
            "runtime_lookahead_violation": recovery["exec_result"].get("error_type")
            == "lookahead_violation",
        }

    async def _run_anomaly_check(
        self, metrics_obj, trades: List[dict], config_obj: BacktestConfig, market_data
    ) -> List[QualityGateResult]:
        from investment_team.strategy_lab.quality_gates.backtest_anomaly import (
            BacktestAnomalyDetector,
        )

        return BacktestAnomalyDetector().check(
            metrics_obj,
            [_trade_from_dict(t) for t in trades],
            dsr_aware=config_obj.walk_forward_enabled,
            coverage_report=metrics_obj.coverage_report,
            market_data={
                sym: [_bar_from_dict(b) for b in bars] for sym, bars in market_data.items()
            },
        )

    async def _handle_critical_anomalies(
        self,
        *,
        spec_dict: dict,
        code: str,
        trades: List[dict],
        metrics: dict,
        exec_result: dict,
        market_data: Dict[str, List[dict]],
        config_dict: dict,
        critical_anomalies: List[QualityGateResult],
        all_gate_results: List[dict],
        refinement_attempts: List[str],
        zero_trade_attempts: List[str],
        round_num: int,
        drift: _DriftState,
    ) -> Dict[str, Any]:
        failure_details = "\n".join(f"- {g.details}" for g in critical_anomalies)
        diagnostics = exec_result.get("execution_diagnostics")
        zero_trade_category = diagnostics.get("zero_trade_category") if diagnostics else None

        if zero_trade_category == "ENTRY_WITH_NO_EXIT":
            raise SpecImplementabilityError(
                (
                    "ENTRY_WITH_NO_EXIT: entries filled but engine-owned exits never fired "
                    "in the test window. Revise spec.exit_rules — loosen or retune the "
                    f"stop-loss / take-profit / signal-exit rules so exits can fire. "
                    f"Diagnostics:\n{failure_details}"
                ),
                failure_phase="evaluation",
                last_spec=StrategySpec.parse_persisted(spec_dict),
                last_code=code,
            )

        if zero_trade_category is not None:
            zt = await self._try_zero_trade_repair(
                spec_dict=spec_dict,
                code=code,
                diagnostics=diagnostics,
                market_data=market_data,
                config_dict=config_dict,
                zero_trade_attempts=zero_trade_attempts,
                round_num=round_num,
                all_gate_results=all_gate_results,
                drift=drift,
            )
            if zt is not None:
                return zt

        new_spec, new_code, exhausted = await self._refine_or_exhaust(
            spec_dict=spec_dict,
            code=code,
            failure_phase="evaluation",
            refine_label="evaluation (backtest anomaly)",
            failure_details=failure_details,
            metrics=metrics,
            refinement_attempts=refinement_attempts,
            round_num=round_num,
            default_change_label="anomaly fix",
            drift=drift,
            consecutive_spec_mutation_rounds={},
        )
        return {
            "spec": new_spec,
            "code": new_code,
            "trades": trades,
            "metrics": metrics,
            "exec_result": exec_result,
            "exhausted": exhausted,
            "ran_on_non_conforming_code": None,
        }

    async def _try_zero_trade_repair(
        self,
        *,
        spec_dict: dict,
        code: str,
        diagnostics: dict,
        market_data: Dict[str, List[dict]],
        config_dict: dict,
        zero_trade_attempts: List[str],
        round_num: int,
        all_gate_results: List[dict],
        drift: _DriftState,
    ) -> Optional[Dict[str, Any]]:
        """Decomposed port of ``ZeroTradeRepairer.try_repair`` (zero_trade_repair.py)."""
        spec_obj = StrategySpec.parse_persisted(spec_dict)
        try:
            report = await _exec(
                "zero_trade_repair_activity",
                spec=spec_dict,
                code=code,
                diagnostics=diagnostics,
                prior_attempts=zero_trade_attempts,
            )
        except Exception:
            zero_trade_attempts.append("agent_error: zero-trade repair failed")
            return None
        if not report.get("proposed_code"):
            zero_trade_attempts.append(
                f"no_proposal ({report.get('root_cause_category')}): "
                f"{(report.get('evidence') or 'agent declined to propose')[:160]}"
            )
            return None

        safety_gates = CodeSafetyChecker().check(report["proposed_code"], spec_obj)
        _record_gates(
            safety_gates, [], refinement_round=round_num, gate_name_prefix="zero_trade_repair_"
        )
        if any(not g.passed and g.severity == "critical" for g in safety_gates):
            zero_trade_attempts.append("unsafe_code (zero-trade repair rejected)")
            all_gate_results.extend(g.model_dump(mode="json") for g in safety_gates)
            return None

        updates = report.get("proposed_spec_updates") or {}
        data = spec_obj.model_dump()
        if "risk_limits" in updates:
            data["risk_limits"] = updates["risk_limits"]
        data["strategy_code"] = report["proposed_code"]
        proposed_spec = StrategySpec.model_validate(data)

        post_repair_spec_gates: List[QualityGateResult] = []
        if updates:
            post_repair_spec_gates = StrategySpecValidator().validate(
                proposed_spec, phase="synthesis"
            )
            _record_gates(
                post_repair_spec_gates,
                [],
                refinement_round=round_num,
                gate_name_prefix="zero_trade_repair_",
            )
        if any(not g.passed and g.severity == "critical" for g in post_repair_spec_gates):
            zero_trade_attempts.append("invalid_spec_after_repair (zero-trade repair rejected)")
            all_gate_results.extend(g.model_dump(mode="json") for g in post_repair_spec_gates)
            return None

        repair_exec = await _exec(
            "run_strategy_code_activity",
            strategy_code=report["proposed_code"],
            market_data=market_data,
            config=config_dict,
            strategy=proposed_spec.model_dump(mode="json"),
        )
        if not repair_exec["success"]:
            zero_trade_attempts.append(f"reexec_failed: {repair_exec.get('error_type')}")
            return None

        config_obj = BacktestConfig(**config_dict)
        new_metrics = compute_metrics(
            [_trade_from_dict(t) for t in repair_exec["trades"]],
            config_obj.initial_capital,
            config_obj.start_date,
            config_obj.end_date,
        )
        new_anomaly_gates = await self._run_anomaly_check(
            new_metrics, repair_exec["trades"], config_obj, market_data
        )
        _record_gates(
            new_anomaly_gates, [], refinement_round=round_num, gate_name_prefix="zero_trade_repair_"
        )
        if any(not g.passed and g.severity == "critical" for g in new_anomaly_gates):
            zero_trade_attempts.append("anomaly_after_repair (zero-trade repair rejected)")
            all_gate_results.extend(g.model_dump(mode="json") for g in new_anomaly_gates)
            return None

        change_summary = report.get("changes_made") or f"repair {report.get('root_cause_category')}"
        zero_trade_attempts.append(
            f"committed ({report.get('root_cause_category')}): {change_summary[:160]}"
        )
        drift.record_spec_change(
            phase="verification",
            agent="ZeroTradeRepairer",
            before_spec=spec_obj,
            after_spec=proposed_spec,
            reason=change_summary,
        )
        drift.record_code_change(
            phase="verification",
            agent="ZeroTradeRepairer",
            before_code=code,
            after_code=report["proposed_code"],
            reason=change_summary,
        )
        conformance_gates = PredicateConformanceGate().check(
            report["proposed_code"],
            proposed_spec,
            phase="verification",
            attempt=_code_conformance_retries(),
        )
        ran_on_non_conforming = _round_demoted_conformance(conformance_gates)
        all_gate_results.extend(g.model_dump(mode="json") for g in safety_gates)
        all_gate_results.extend(g.model_dump(mode="json") for g in post_repair_spec_gates)
        all_gate_results.extend(g.model_dump(mode="json") for g in new_anomaly_gates)
        all_gate_results.extend(g.model_dump(mode="json") for g in conformance_gates)
        return {
            "spec": proposed_spec.model_dump(mode="json"),
            "code": report["proposed_code"],
            "trades": repair_exec["trades"],
            "metrics": new_metrics.model_dump(mode="json"),
            "exec_result": repair_exec,
            "exhausted": False,
            "ran_on_non_conforming_code": ran_on_non_conforming,
        }

    async def _refine_or_exhaust(
        self,
        *,
        spec_dict: dict,
        code: str,
        failure_phase: str,
        failure_details: str,
        metrics: Optional[dict],
        refinement_attempts: List[str],
        round_num: int,
        default_change_label: str,
        drift: _DriftState,
        consecutive_spec_mutation_rounds: Dict[str, int],
        refine_label: Optional[str] = None,
    ) -> Tuple[dict, str, bool]:
        if round_num >= MAX_CODE_REFINEMENT_ROUNDS - 1:
            return spec_dict, code, True

        try:
            refined = await _exec(
                "refinement_activity",
                spec=spec_dict,
                code=code,
                failure_phase=refine_label or failure_phase,
                failure_details=failure_details,
                metrics=metrics,
                prior_attempts=refinement_attempts,
            )
            updates, new_code = refined["updated_fields"], refined["updated_code"]
        except Exception:
            updates, new_code = {"changes_made": "refinement failed — no changes"}, code

        new_spec_dict = _apply_updates(
            spec_dict,
            updates,
            new_code,
            failure_phase=failure_phase,
            consecutive_spec_mutation_rounds=consecutive_spec_mutation_rounds,
        )
        changes = updates.get("changes_made", default_change_label)
        refinement_attempts.append(changes)
        drift.record_code_change(
            phase="synthesis",
            agent="RefinementAgent",
            before_code=code,
            after_code=new_code,
            reason=changes,
        )
        drift.record_spec_change(
            phase="synthesis",
            agent="RefinementAgent",
            before_spec=StrategySpec.parse_persisted(spec_dict),
            after_spec=StrategySpec.parse_persisted(new_spec_dict),
            reason=changes,
        )
        return new_spec_dict, new_code, False

    # ------------------------------------------------------------------
    # Trade alignment loop (ports _run_trade_alignment_loop)
    # ------------------------------------------------------------------

    async def _run_trade_alignment_loop(
        self,
        *,
        spec_dict: dict,
        code: str,
        trades: List[dict],
        metrics: dict,
        market_data: Optional[Dict[str, List[dict]]],
        config_dict: dict,
        execution_succeeded: bool,
        all_gate_results: List[dict],
        ran_on_non_conforming_code: bool,
        drift: _DriftState,
        backtest_cache: Dict[str, dict],
    ) -> Dict[str, Any]:
        alignment_attempts: List[str] = []
        alignment_reports: List[dict] = []

        if not (execution_succeeded and trades and market_data is not None):
            return {
                "spec": spec_dict,
                "code": code,
                "trades": trades,
                "metrics": metrics,
                "alignment_attempts": alignment_attempts,
                "alignment_reports": alignment_reports,
                "trades_aligned": False,
                "ran_on_non_conforming_code": ran_on_non_conforming_code,
            }

        for align_round in range(MAX_ALIGNMENT_ROUNDS):
            audit = await _exec(
                "run_alignment_audit_activity",
                spec=spec_dict,
                code=code,
                trades=trades,
                metrics=metrics,
                prior_attempts=alignment_attempts,
                market_data=market_data,
                config=config_dict,
            )
            report = audit["report"]
            alignment_reports.append(report)
            all_gate_results.extend(audit["gate_results"])
            all_gate_results.append(
                _build_orchestrator_gate(
                    "trade_alignment",
                    phase="verification",
                    severity="info" if report["aligned"] else "critical",
                    details=report.get("rationale", ""),
                    refinement_round=align_round,
                ).model_dump(mode="json")
            )

            if report["aligned"]:
                break
            if not report.get("proposed_code"):
                break
            if align_round >= MAX_ALIGNMENT_ROUNDS - 1:
                break

            spec_obj = StrategySpec.parse_persisted(spec_dict)
            try:
                proposed_spec = spec_obj.model_copy(
                    update={"strategy_code": report["proposed_code"]}
                )
            except SpecImplementabilityError:
                break
            safety_gates = CodeSafetyChecker().check(
                report["proposed_code"], proposed_spec, phase="verification"
            )
            if any(not g.passed and g.severity == "critical" for g in safety_gates):
                break

            cache_key = _backtest_cache_key(
                report["proposed_code"],
                market_data,
                config_dict,
                proposed_spec.model_dump(mode="json"),
            )
            if cache_key in backtest_cache:
                align_exec = backtest_cache[cache_key]
            else:
                align_exec = await _exec(
                    "run_strategy_code_activity",
                    strategy_code=report["proposed_code"],
                    market_data=market_data,
                    config=config_dict,
                    strategy=spec_dict,
                )
                backtest_cache[cache_key] = align_exec
            if not align_exec["success"]:
                break

            config_obj = BacktestConfig(**config_dict)
            new_metrics = compute_metrics(
                [_trade_from_dict(t) for t in align_exec["trades"]],
                config_obj.initial_capital,
                config_obj.start_date,
                config_obj.end_date,
            )
            anomaly_gates = await self._run_anomaly_check(
                new_metrics, align_exec["trades"], config_obj, market_data
            )
            _record_gates(
                anomaly_gates,
                all_gate_results,
                refinement_round=align_round,
                gate_name_prefix="alignment_",
            )
            if any(not g.passed and g.severity == "critical" for g in anomaly_gates):
                break

            conformance_gates = PredicateConformanceGate().check(
                report["proposed_code"],
                proposed_spec,
                phase="verification",
                attempt=_code_conformance_retries(),
            )
            _record_gates(
                conformance_gates,
                all_gate_results,
                refinement_round=align_round,
                gate_name_prefix="alignment_",
            )
            ran_on_non_conforming_code = _round_demoted_conformance(conformance_gates)

            alignment_attempts.append(report.get("changes_made") or "alignment fix")
            drift.record_code_change(
                phase="verification",
                agent="TradeAlignmentAgent",
                before_code=code,
                after_code=report["proposed_code"],
                reason=report.get("changes_made", "alignment fix"),
            )
            drift.record_spec_change(
                phase="verification",
                agent="TradeAlignmentAgent",
                before_spec=spec_obj,
                after_spec=proposed_spec,
                reason=report.get("changes_made", "alignment fix"),
            )
            spec_dict = proposed_spec.model_dump(mode="json")
            code = report["proposed_code"]
            trades = align_exec["trades"]
            metrics = new_metrics.model_dump(mode="json")

        last_report = alignment_reports[-1] if alignment_reports else None
        trades_aligned = bool(last_report and last_report["aligned"])
        return {
            "spec": spec_dict,
            "code": code,
            "trades": trades,
            "metrics": metrics,
            "alignment_attempts": alignment_attempts,
            "alignment_reports": alignment_reports,
            "trades_aligned": trades_aligned,
            "ran_on_non_conforming_code": ran_on_non_conforming_code,
        }


def _format_regression_notice_dict(critique: dict, regressed: set) -> str:
    """Dict-shaped stand-in for ``_format_regression_notice`` (which takes a SpecCritique)."""
    if not regressed:
        return ""
    lines = [
        f"- {i['field']}: {i['description']}"
        for i in critique["issues"]
        if i["issue_id"] in regressed
    ]
    return "Regressions — issues previously resolved that have reappeared:\n" + "\n".join(lines)


def _apply_updates(
    spec_dict: dict,
    updates: dict,
    code: str,
    *,
    failure_phase: Optional[str],
    consecutive_spec_mutation_rounds: Dict[str, int],
) -> dict:
    """Workflow-local port of ``StrategyLabOrchestrator._apply_updates``."""
    spec_obj = StrategySpec.parse_persisted(spec_dict)
    data = spec_obj.model_dump()
    data["strategy_code"] = code

    stray = set(updates) - _REFINEMENT_ALLOWED_KEYS - _REFINEMENT_PASSTHROUGH_KEYS
    risk_limits_proposed = updates.get("risk_limits")
    if risk_limits_proposed is not None:
        merged_limits, loosened, unknown = _merge_risk_limits_tighten_only(
            spec_obj.risk_limits, risk_limits_proposed
        )
        if loosened:
            raise SpecImplementabilityError(
                evidence=f"refinement tried to loosen risk_limits fields: {sorted(loosened)}",
                failure_phase=failure_phase,
                last_spec=spec_obj,
                last_code=code,
            )
        data["risk_limits"] = merged_limits.model_dump()

    if stray:
        if failure_phase is not None:
            counter = consecutive_spec_mutation_rounds
            counter[failure_phase] = counter.get(failure_phase, 0) + 1
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
                    last_spec=spec_obj,
                    last_code=code,
                )
    elif failure_phase is not None:
        consecutive_spec_mutation_rounds[failure_phase] = 0

    return StrategySpec.model_validate(data).model_dump(mode="json")


def _backtest_cache_key(code: str, market_data: dict, config: dict, spec: dict) -> str:
    """Workflow-local port of ``BacktestCache._key`` (memoization within one design attempt)."""
    import hashlib
    import json

    from investment_team.strategy_lab.phases import hash_code, hash_spec

    if _is_nondeterministic_code(code):
        return f"__nondeterministic__:{workflow.uuid4()}"

    md_fingerprint = hashlib.sha256(
        json.dumps(sorted(market_data.keys()), sort_keys=True).encode("utf-8")
        + json.dumps(
            {
                sym: [bars[0], bars[-1], len(bars)] if bars else []
                for sym, bars in market_data.items()
            },
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    config_hash = hashlib.sha256(
        json.dumps(config, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
    digest = hashlib.sha256()
    digest.update(hash_code(code).encode("utf-8"))
    digest.update(b"\x00")
    digest.update(md_fingerprint.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(config_hash.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(hash_spec(StrategySpec.parse_persisted(spec)).encode("utf-8"))
    return digest.hexdigest()


def _is_nondeterministic_code(code: str) -> bool:
    """Workflow-local port of ``backtest_cache._is_nondeterministic``."""
    import ast

    nondeterministic_modules = frozenset({"random", "time", "datetime", "secrets", "uuid", "os"})
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return True
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name.split(".")[0] in nondeterministic_modules for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] in nondeterministic_modules:
                return True
    return False


def _trade_from_dict(d: dict):
    from investment_team.models import TradeRecord

    return TradeRecord(**d)


def _bar_from_dict(d: dict):
    from investment_team.market_data_service import OHLCVBar

    return OHLCVBar(**d)


WORKFLOWS = [StrategyLabCycleWorkflow]
ACTIVITIES = act.ACTIVITIES
TASK_QUEUE = "strategy-lab-queue"
WORKFLOW_ID_PREFIX = "strategy-lab-cycle-"

__all__ = [
    "ACTIVITIES",
    "StrategyLabCycleWorkflow",
    "TASK_QUEUE",
    "WORKFLOWS",
    "WORKFLOW_ID_PREFIX",
]
