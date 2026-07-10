"""Temporal workflow for one Strategy Lab cycle.

``StrategyLabCycleWorkflow`` is a thin, durable driver for the *outer*
design-re-entry loop of ``StrategyLabOrchestrator.run_cycle``
(backend/agents/investment_team/strategy_lab/orchestrator.py). It deliberately
does **not** re-express the per-attempt phase pipeline (design → synthesis →
refinement/alignment → verification/analysis → record assembly) as sandboxed
workflow code: that whole pipeline runs verbatim inside
``run_design_attempt_activity`` (activities.py), which wraps the original
``_run_design_attempt`` method unmodified.

Why the attempt-level boundary rather than one activity per phase:
  * The per-attempt phases hand large intermediate bundles between each other
    in local Python (the spec, the code, the synthesis bundle including
    ``market_data``, the alignment bundle) and reset attempt-scoped instance
    state (``_backtest_cache``, ``_consecutive_spec_mutation_rounds``) at the
    top of ``_run_design_attempt``. None of that needs to cross an activity
    boundary, so a single boundary avoids re-serializing it four times over.
  * Quality gates construct ``QualityGateResult`` (whose ``evaluated_at`` field
    calls ``datetime.now``) and several read ``os.environ`` — both illegal in
    the temporalio workflow sandbox at runtime. Running the whole attempt in an
    activity (outside the sandbox) resolves that structurally, rather than
    hand-porting ~600 lines of loop logic and its determinism hazards.

The workflow therefore reproduces only ``run_cycle``'s ~40-line outer body:
gather the convergence directives (pure tracker reads), loop
``range(MAX_DESIGN_REENTRIES + 1)`` calling the attempt activity, branch on its
structured ``{"kind": "record" | "reentry", ...}`` outcome exactly where
``run_cycle`` branches on the ``except SpecImplementabilityError``, and on
re-entry exhaustion assemble the ``failed: spec_unimplementable`` short-circuit
record. Drift copy-on-entry / merge-on-completion across attempts is plain dict
work here: ``_DriftCollector.snapshot()`` hands each attempt a fresh *empty*
child collector by design, so copy-on-entry is simply an empty drift dict, and
merge-on-completion is a list ``extend``.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, List, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy

from investment_team.strategy_lab.temporal import activities as act
from investment_team.strategy_lab.temporal.dto import (
    convergence_tracker_from_wire,
    convergence_tracker_to_wire,
)

# ``MAX_DESIGN_REENTRIES`` (a plain ``orchestrator`` module constant) is
# threaded in via ``resolve_workflow_config_activity`` rather than imported
# here: importing ``orchestrator`` at this module's top would drag its whole
# transitive graph (strands, market-data providers, every agent/gate) into the
# temporalio sandbox's restricted re-import of this workflow module. This
# fallback is used only if the config activity somehow omits the value.
_MAX_DESIGN_REENTRIES_FALLBACK = 2

# Bounded retry backstop. The design-attempt activity's in-body LLM envelope
# already owns its own retry/backoff for LLM transients, so a Temporal-level
# retry only recovers a genuine worker crash mid-activity; keep it small since
# re-running a whole design attempt is expensive.
_ACTIVITY_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=2,
)
_ACTIVITY_TIMEOUT = timedelta(minutes=10)
# One design attempt runs the whole per-cycle phase pipeline (up to
# ``STRATEGY_LAB_DESIGN_MAX_LLM_CALLS`` model round-trips plus backtests), so it
# needs a far wider ceiling than a single LLM/gate/persist activity.
_DESIGN_ATTEMPT_TIMEOUT = timedelta(hours=2)


async def _exec(
    fn: Any,
    /,
    *,
    params: Optional[Dict[str, Any]] = None,
    timeout: timedelta = _ACTIVITY_TIMEOUT,
) -> Any:
    """Thin ``workflow.execute_activity`` wrapper.

    Takes the activity function *object* directly (e.g. ``act.run_design_attempt_activity``)
    rather than a string name, so a typo is a ``NameError`` at import/analysis
    time instead of a runtime failure.

    Preconditions:
        ``fn`` is an ``@activity.defn``-decorated function from the
        ``activities`` module. ``params`` is the single positional dict the
        activity expects, or ``None`` for a no-argument activity.
    Postconditions:
        Returns the activity's result, retried per ``_ACTIVITY_RETRY``.
    """
    args = [params] if params is not None else []
    return await workflow.execute_activity(
        fn,
        args=args,
        start_to_close_timeout=timeout,
        retry_policy=_ACTIVITY_RETRY,
    )


def _empty_drift() -> Dict[str, List[dict]]:
    """Return a fresh empty drift wire dict (a ``_DriftCollector.snapshot()``).

    Postconditions:
        Returns ``{"spec_history": [], "code_history": [], "gate_timeline": []}``
        — three independent lists, so merging a child into it never aliases.
    """
    return {"spec_history": [], "code_history": [], "gate_timeline": []}


@workflow.defn(name="StrategyLabCycleWorkflow")
class StrategyLabCycleWorkflow:
    """Durable per-cycle Strategy Lab workflow (thin re-entry-loop driver).

    Preconditions:
        ``cycle_input`` (the sole ``run()`` argument) is a JSON-shaped dict:
        ``prior_records`` (list of ``StrategyLabRecord`` dumps), ``config``
        (``BacktestConfig`` dump), ``signal_brief`` (dump or ``None``),
        ``exclude_asset_classes`` (list or ``None``),
        ``convergence_tracker_state`` (``dto`` wire dict — the batch-level
        tracker), and optionally ``workflow_config`` (a
        ``resolve_workflow_config_activity`` result; resolved via an activity
        call when absent — only its ``regime_summary_enabled`` flag is read
        here).
    Postconditions:
        Returns ``{"record": StrategyLabRecord dump, "convergence_tracker_state":
        <updated dto wire dict>}``, mirroring ``run_cycle``'s return plus the
        batch-level tracker state the parent batch workflow merges.
    Invariants:
        Exactly one ``run_design_attempt_activity`` call happens per design
        attempt; the LLM-call budget, gate-result accumulation, and tracker
        state are threaded attempt→attempt so their ceilings/history span the
        whole cycle, never resetting per attempt.
    """

    @workflow.run
    async def run(self, cycle_input: Dict[str, Any]) -> Dict[str, Any]:
        prior_records = cycle_input["prior_records"]
        config_dict = cycle_input["config"]
        signal_brief = cycle_input.get("signal_brief")
        exclude_asset_classes = cycle_input.get("exclude_asset_classes")
        tracker_state = cycle_input.get("convergence_tracker_state") or {}

        # Gather convergence directives once from the batch-level tracker
        # (pure counter/set reads — safe in the sandbox), appended to on each
        # re-entry below. Mirrors run_cycle's directive gathering.
        directives: List[str] = []
        directive_tracker = convergence_tracker_from_wire(tracker_state)
        stall_dir = directive_tracker.get_stall_directive()
        if stall_dir:
            directives.append(stall_dir)
        diversity_dir = directive_tracker.get_diversity_directive()
        if diversity_dir:
            directives.append(diversity_dir)
        directives.extend(directive_tracker.get_failure_directives())

        # Market-regime read, computed once per cycle and shared across every
        # re-entry (fail-open: ``None`` when disabled or on data failure). The
        # on/off gate is the workflow's responsibility, so resolve the config
        # flag once (or reuse a batch-provided one).
        wf_config = cycle_input.get("workflow_config")
        if wf_config is None:
            wf_config = await _exec(act.resolve_workflow_config_activity)
        regime_summary = None
        if wf_config.get("regime_summary_enabled"):
            regime_summary = await _exec(act.compute_regime_summary_activity)
        # Re-entry bound threaded from the config activity (see the
        # module-level note) rather than imported from ``orchestrator``.
        max_reentries = int(wf_config.get("max_design_reentries", _MAX_DESIGN_REENTRIES_FALLBACK))

        # Parent commit log for drift across attempts. Each attempt works on a
        # fresh empty child (copy-on-entry); the child is folded back in only
        # once the attempt fails (commit-on-completion), so a failed attempt's
        # revisions never poison the next attempt while still surviving into
        # the short-circuit diagnostic record.
        parent_drift = _empty_drift()
        cumulative_gate_results: List[dict] = []
        budget_calls = 0
        phase_back_count = 0

        last_evidence: Optional[str] = None
        last_spec_dict: Optional[dict] = None
        last_code: str = ""
        last_failure_phase: Optional[str] = None
        last_design_context: Optional[dict] = None

        for design_attempt in range(max_reentries + 1):
            outcome = await _exec(
                act.run_design_attempt_activity,
                params={
                    "prior_records": prior_records,
                    "config": config_dict,
                    "signal_brief": signal_brief,
                    "exclude_asset_classes": exclude_asset_classes,
                    "directives": directives,
                    "design_attempt": design_attempt,
                    "phase_back_count": phase_back_count,
                    # Copy-on-entry: a fresh empty child collector for this attempt.
                    "drift": _empty_drift(),
                    "gate_results": cumulative_gate_results,
                    "budget_calls": budget_calls,
                    "regime_summary": regime_summary,
                    "convergence_tracker_state": tracker_state,
                },
                timeout=_DESIGN_ATTEMPT_TIMEOUT,
            )
            # Thread the whole-cycle accumulators forward regardless of outcome.
            tracker_state = outcome["convergence_tracker_state"]
            cumulative_gate_results = outcome["gate_results"]
            budget_calls = outcome["budget_calls"]

            if outcome["kind"] == "record":
                return {
                    "record": outcome["record"],
                    "convergence_tracker_state": tracker_state,
                }

            # ``reentry``: the attempt raised SpecImplementabilityError.
            last_evidence = outcome["evidence"]
            last_spec_dict = outcome["last_spec"]
            last_code = outcome["last_code"] or ""
            last_failure_phase = outcome["failure_phase"]
            last_design_context = outcome["design_context"]
            phase_back_count += 1
            # Each failed attempt consumed real LLM work on the same evaluation
            # window, so advance the DSR trial counter by one per phase-back.
            tracker = convergence_tracker_from_wire(tracker_state)
            tracker.increment_trials(1)
            tracker_state = convergence_tracker_to_wire(tracker)
            # Commit-on-completion: fold this attempt's child drift into the
            # parent so the short-circuit record retains its diagnostics.
            child_drift = outcome["drift"]
            parent_drift["spec_history"].extend(child_drift["spec_history"])
            parent_drift["code_history"].extend(child_drift["code_history"])
            parent_drift["gate_timeline"].extend(child_drift["gate_timeline"])
            if design_attempt >= max_reentries:
                break
            directives.append(f"PREVIOUS SPEC UNIMPLEMENTABLE: {last_evidence}")

        # Re-entry budget exhausted. ``last_spec``/``last_evidence`` are set by
        # every SpecImplementabilityError raiser; guard defensively in case a
        # future raiser violates that contract.
        if last_spec_dict is None or last_evidence is None:
            raise RuntimeError(
                "SpecImplementabilityError raised without last_spec/evidence; "
                "cannot build short-circuit record. This is a bug in a refinement "
                "code path; please file an issue with the run logs."
            )
        result = await _exec(
            act.build_short_circuit_record_activity,
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
                    f"Spec unimplementable after {max_reentries + 1} design attempts "
                    f"(last failure_phase={last_failure_phase}): {last_evidence}"
                ),
                "design_context": last_design_context,
                "phase_back_count": phase_back_count,
                "drift_collector": parent_drift,
                "convergence_tracker_state": tracker_state,
            },
        )
        return {
            "record": result["record"],
            "convergence_tracker_state": result["convergence_tracker_state"],
        }


# Dedicated task queue for the fine-grained Strategy Lab workers, kept separate
# from ``investment_team/temporal``'s ``investment-queue`` so a multi-hundred-
# cycle batch never head-of-line-blocks ad hoc single backtests and each can be
# tuned/scaled independently. A plain string constant — sandbox-safe.
TASK_QUEUE = "strategy-lab-queue"

WORKFLOWS = [StrategyLabCycleWorkflow]
ACTIVITIES = act.ACTIVITIES
