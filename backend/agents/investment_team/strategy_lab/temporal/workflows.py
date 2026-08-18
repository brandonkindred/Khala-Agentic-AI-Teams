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

This module also defines ``StrategyLabBatchWorkflow`` — the durable parent that
ports ``_strategy_lab_worker``'s batch/wave loop and fans each batch's cycles out
as ``StrategyLabCycleWorkflow`` **child workflows** (a wave's worth started
concurrently), merging their results into the batch-level convergence tracker in
cycle-index order. Both run on the dedicated ``TASK_QUEUE`` (``strategy-lab-queue``)
and are exported via ``WORKFLOWS``; ``ACTIVITIES`` re-exports the full
``activities.ACTIVITIES`` list the worker registers alongside them. See the
``StrategyLabBatchWorkflow`` class docstring for the batch-input/output contract.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any, Dict, List, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

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

# Mirrors ``strategy_lab.run_state.DEFAULT_FENCING_GENERATION`` -- duplicated
# rather than imported for the same reason as ``_MAX_DESIGN_REENTRIES_FALLBACK``
# above: ``run_state`` isn't imported anywhere else in this module (even
# ``activities.py``, which runs unsandboxed, only imports it locally inside
# function bodies), and this module's own top-level code runs inside the
# temporalio workflow sandbox, where a module-level import's side effects
# (e.g. ``run_state``'s ``threading.Lock()``) are best avoided.
_DEFAULT_FENCING_GENERATION = 1

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
# Server-enforced liveness deadline for the design-attempt activity's
# heartbeat (activities.py wraps the attempt in a fixed-interval
# BackgroundHeartbeat, decoupled from ``emit`` checkpoint cadence -- see
# ``_DESIGN_ATTEMPT_HEARTBEAT_INTERVAL_S`` there). Sized generously relative
# to that fixed interval (not to the pipeline's own uneven cadence) so a
# missed heartbeat window is a real liveness problem, not a slow-but-healthy
# attempt: missing it fails/retries the WHOLE up-to-2-hour attempt.
_DESIGN_ATTEMPT_HEARTBEAT_TIMEOUT = timedelta(seconds=90)

# A cycle child workflow is expensive and its own activities already retry
# internally, so a failed cycle is not re-run at the child level — it surfaces
# as an errored cycle, exactly as a raising ``_run_one_strategy_lab_cycle`` does
# in thread mode.
_CHILD_RETRY = RetryPolicy(maximum_attempts=1)
# A whole cycle (design re-entries × phase pipeline) can run for hours; bound it
# generously rather than leaving it unbounded.
_CHILD_EXECUTION_TIMEOUT = timedelta(hours=8)

# Matches thread mode's ``_ERRORED_DETAILS_MAX`` (api/main.py) — bounds memory
# for the per-failure diagnostic list. Duplicated rather than imported from
# ``api.main`` for the same reason as ``_MAX_DESIGN_REENTRIES_FALLBACK`` above:
# importing api.main's transitive graph into this module would break the
# temporalio sandbox's restricted re-import of workflow code.
_ERRORED_DETAILS_MAX = 50


async def _exec(
    fn: Any,
    /,
    *,
    params: Optional[Dict[str, Any]] = None,
    timeout: timedelta = _ACTIVITY_TIMEOUT,
    heartbeat_timeout: Optional[timedelta] = None,
) -> Any:
    """Thin ``workflow.execute_activity`` wrapper.

    Takes the activity function *object* directly (e.g. ``act.run_design_attempt_activity``)
    rather than a string name, so a typo is a ``NameError`` at import/analysis
    time instead of a runtime failure.

    Preconditions:
        ``fn`` is an ``@activity.defn``-decorated function from the
        ``activities`` module. ``params`` is the single positional dict the
        activity expects, or ``None`` for a no-argument activity.
        ``heartbeat_timeout`` is ``None`` (the default -- no heartbeat
        deadline, matching every non-heartbeating activity) unless ``fn``
        heartbeats itself (currently only ``run_design_attempt_activity``).
    Postconditions:
        Returns the activity's result, retried per ``_ACTIVITY_RETRY``.
    """
    args = [params] if params is not None else []
    return await workflow.execute_activity(
        fn,
        args=args,
        start_to_close_timeout=timeout,
        heartbeat_timeout=heartbeat_timeout,
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
        call when absent — its ``regime_summary_enabled`` flag and
        ``max_design_reentries`` value are read here), ``run_id`` (the owning
        run's id -- absent/``None`` disables design-attempt checkpointing for
        every attempt in this cycle, see ``ADR-012``), ``cycle_index`` (this
        cycle's 0-based index within the run -- absent/``None`` disables live
        SSE progress publishing for every attempt in this cycle, since
        ``StrategyLabProgressEvent.cycle_index`` is a required field on the
        frontend and a malformed event is worse than none), and ``generation``
        (int, default ``_DEFAULT_FENCING_GENERATION`` -- the fencing
        generation this cycle's incarnation was dispatched with). All three
        are ``.get(...)``-guarded so a ``cycle_input`` from a workflow-history
        replay predating these fields still runs (with checkpointing/progress
        publishing simply disabled).
    Postconditions:
        Returns ``{"record": StrategyLabRecord dump, "convergence_tracker_state":
        <updated dto wire dict>}`` on a terminal record, mirroring ``run_cycle``'s
        return plus the batch-level tracker state the parent batch workflow
        merges, or ``{"kind": "skipped", "convergence_tracker_state": ...}``
        when the attempt activity reported no market data available (no
        ``"record"`` key in that case — the parent workflow branches on
        ``result.get("kind") == "skipped"``).
    Invariants:
        Exactly one ``run_design_attempt_activity`` call happens per design
        attempt; the LLM-call budget, gate-result accumulation, and tracker
        state are threaded attempt→attempt so their ceilings/history span the
        whole cycle, never resetting per attempt. The re-entry loop bound
        (``max_reentries``) is resolved once, from ``wf_config``'s
        ``max_design_reentries`` (falling back to
        ``_MAX_DESIGN_REENTRIES_FALLBACK`` when absent), and stays constant
        for every attempt in the cycle.
    """

    @workflow.run
    async def run(self, cycle_input: Dict[str, Any]) -> Dict[str, Any]:
        prior_records = cycle_input["prior_records"]
        config_dict = cycle_input["config"]
        signal_brief = cycle_input.get("signal_brief")
        exclude_asset_classes = cycle_input.get("exclude_asset_classes")
        tracker_state = cycle_input.get("convergence_tracker_state") or {}
        run_id = cycle_input.get("run_id")
        # Absent for a cycle_input from a workflow-history replay predating
        # this field -- run_design_attempt_activity's progress-publish
        # closure treats a missing cycle_index as "skip publishing" rather
        # than sending a malformed event, so this is safe to leave None.
        cycle_index = cycle_input.get("cycle_index")
        generation = int(cycle_input.get("generation", _DEFAULT_FENCING_GENERATION))
        # Per-batch cache key threaded from the parent batch workflow; forwarded
        # verbatim to run_design_attempt_activity so the worker can resolve the
        # one shared BatchIndicatorCache for this batch (when the flag is on).
        # ``.get`` tolerates old-shaped/resumed inputs that predate this field.
        batch_cache_key = cycle_input.get("batch_cache_key")

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
                    "run_id": run_id,
                    "generation": generation,
                    "cycle_index": cycle_index,
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
                    "batch_cache_key": batch_cache_key,
                },
                timeout=_DESIGN_ATTEMPT_TIMEOUT,
                heartbeat_timeout=_DESIGN_ATTEMPT_HEARTBEAT_TIMEOUT,
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

            if outcome["kind"] == "skipped":
                # No market data — cycle-terminal immediately, no further
                # design-attempt retry (mirrors thread mode, where the 502
                # isn't design-attempt-scoped).
                return {
                    "kind": "skipped",
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


def _snapshot_tracker_wire(primary_state: Dict[str, Any]) -> Dict[str, Any]:
    """Return a per-cycle snapshot of the batch-level convergence tracker, as wire.

    Mirrors thread-mode's ``primary_tracker.snapshot()`` handed to each concurrent
    cycle: a shallow clone whose ``_trial_count_at_snapshot`` baseline is captured
    so ``merge_wave_results_activity`` later folds only the per-cycle trial delta
    back in. Pure ``ConvergenceTracker`` operations (list/Counter copies + ints) —
    sandbox-safe, so this runs in workflow code rather than an activity.

    Preconditions:
        ``primary_state`` is a ``dto`` wire dict (or ``{}`` for a fresh tracker).
    Postconditions:
        Returns a wire dict for an isolated snapshot equivalent to
        ``convergence_tracker_from_wire(primary_state).snapshot()``.
    """
    return convergence_tracker_to_wire(convergence_tracker_from_wire(primary_state).snapshot())


def _terminal_sse_event(
    *,
    status: str,
    completed_count: int,
    skipped_count: int,
    errored_count: int,
    errored_details: List[Dict[str, Any]],
    completed_batches: int,
    total_batches: int,
) -> Dict[str, Any]:
    """Build the terminal SSE event payload for ``StrategyLabBatchWorkflow.run``'s status.

    Maps ``status`` onto the three terminal event shapes the frontend's
    ``StrategyLabStreamEvent`` union models
    (user-interface/src/app/models/investment.model.ts):
    ``StrategyLabCancelledEvent`` (``status == "cancelled"``),
    ``StrategyLabErrorDetailEvent`` (``status`` in
    ``{"failed", "interrupted"}``), or ``StrategyLabCompleteEvent`` (``status``
    in ``{"completed", "completed_with_errors"}``) -- pure dict construction,
    no I/O, so this runs directly in workflow code rather than an activity.

    Preconditions:
        ``status`` is one of the five values ``StrategyLabBatchWorkflow.run``'s
        own ``status`` local can hold.
    Postconditions:
        Returns a JSON-shaped dict with a ``"type"`` key matching one of the
        three event shapes above.
    """
    if status == "cancelled":
        return {"type": "cancelled", "detail": "Run cancelled."}
    if status in ("failed", "interrupted"):
        return {"type": "error", "detail": f"Run {status}.", "terminal_status": status}
    message = (
        f"Run completed with {errored_count} errored and {skipped_count} skipped cycle(s)."
        if errored_count
        else "Run completed."
    )
    return {
        "type": "complete",
        "message": message,
        "status": status,
        "completed_count": completed_count,
        "skipped_count": skipped_count,
        "errored_count": errored_count,
        "errored_details": errored_details,
        "completed_batches": completed_batches,
        "total_batches": total_batches,
    }


@workflow.defn(name="StrategyLabBatchWorkflow")
class StrategyLabBatchWorkflow:
    """Durable batch driver — ports ``_strategy_lab_worker``'s batch/wave loop.

    Replaces the coarse ``InvestmentStrategyLabWorkflow`` (one activity wrapping
    the whole multi-hour ``_strategy_lab_worker`` thread) with a fine-grained
    parent workflow that fans each batch's cycles out as ``StrategyLabCycleWorkflow``
    **child workflows** — one per cycle, ``_child_wave``'s worth started
    concurrently — reproducing thread-mode's per-wave
    ``ThreadPoolExecutor(max_workers=len(wave_indices))`` concurrency on the
    dedicated ``strategy-lab-queue``.

    Preconditions:
        ``batch_input`` (the sole ``run()`` argument) is a JSON-shaped dict:
          - ``run_id``: str run identifier (used for child-workflow ids, run-state
            persistence, and the cancellation check).
          - ``generation`` (int, default 1): the fencing generation this
            workflow incarnation was dispatched with. Minted fresh by
            ``restart_strategy_lab_run``, carried forward unchanged by
            ``resume_strategy_lab_run``, and defaulting to ``1`` for a fresh
            run. Threaded into every ``persist_run_state_activity`` and
            ``finalize_cycle_record_activity`` call so a write from an
            activity belonging to a superseded incarnation is rejected.
          - ``config``: ``BacktestConfig`` JSON dump, shared by every cycle.
          - ``batch_size`` / ``batch_count`` / ``max_parallel``: ints.
          - ``benchmark_symbol``: str, for the per-batch signal brief.
          - ``exclude_asset_classes``: list or ``None``.
          - ``paper_trading_enabled`` (bool, default True) /
            ``paper_trading_lookback_days`` (int, default 365): forwarded to
            ``finalize_cycle_record_activity``.
          - ``start_cycle_offset`` (int, default 0): resume anchor.
          - ``skipped_cycles`` (int, default 0): resume-seed for cycles
            already skipped (no market data) before this dispatch (from
            ``get_resume_seed_counters``); added to, not overwritten by, new
            skips.
          - ``errored_details`` (optional list, default empty): resume seed for
            the per-failure diagnostic list, carried forward from persisted run
            state (mirrors thread mode's ``errored_details``).
          - ``tracker_merge_error_count`` (int, default 0): resume seed for the
            count of cycles whose tracker merge (not the cycle itself) failed
            (mirrors thread mode's ``tracker_merge_error_count``).
          - ``completed_record_ids`` (optional list, default empty): resume
            seed for the record ids already completed before this dispatch —
            required because ``persist_run_state_activity``'s job-service write
            replaces the ``completed_record_ids`` field's value wholesale
            (it does not append), so without this seed a resume would durably
            truncate the field to only post-resume records on its first
            mid-run persist.
          - ``convergence_tracker_state`` (optional dto wire dict, default fresh):
            the batch-level tracker to seed from (for resume).
          - ``workflow_config`` (optional): a ``resolve_workflow_config_activity``
            result, resolved via that activity when absent and threaded down to
            every cycle so each child need not re-resolve it.
    Postconditions:
        Returns ``{"run_id", "status", "completed_record_ids", "errored_cycles",
        "skipped_cycles", "errored_details", "tracker_merge_error_count",
        "convergence_tracker_state"}``. ``status`` is the exact external stop
        status (``cancelled``/``failed``/``interrupted``) when one was observed
        between waves — never forced to ``cancelled`` for an interrupt/failure —
        else ``completed`` / ``completed_with_errors`` (the latter when ≥1 cycle
        errored). Every completed, non-skipped cycle's record is finalized
        (paper-traded + persisted) via ``finalize_cycle_record_activity`` before
        the batch returns; a cycle whose child workflow reports ``{"kind":
        "skipped", ...}`` (no market data) is counted in ``skipped_cycles``
        instead and never finalized. ``errored_details`` accumulates a capped
        (``_ERRORED_DETAILS_MAX``), structured entry (``cycle_index``,
        ``batch_index``, ``error``, ``exception_type``[, ``reason``]) per failed
        cycle — both a child-workflow failure and a per-record tracker-merge
        failure reported by ``merge_wave_results_activity`` — seeded forward
        from ``batch_input`` and never truncated below what was seeded. A
        tracker-merge failure is isolated to its own record (does not fail the
        wave) but still counts toward ``errored_cycles`` and
        ``tracker_merge_error_count``. ``completed_record_ids`` is seeded
        forward from ``batch_input`` (the pre-resume ids) and extended with
        every newly finalized record's id — never truncated below what was
        seeded. When ``workflow.patched("strategy-lab-sse-run-events")`` is
        ``True`` for this execution (see ``run``'s own inline comment — False
        only when replaying a run already in flight when this behavior
        shipped), also best-effort publishes ``cycle_skipped``/
        ``cycle_complete`` SSE events per cycle and one terminal
        ``complete``/``error``/``cancelled`` event via
        ``publish_run_event_activity`` — a UI side-channel, never required for
        this method's own return value or persisted run state to be correct.
    Invariants:
        Each wave merges its settled cycles into the batch-level tracker in
        cycle-index order (via ``merge_wave_results_activity``), so directives are
        reproducible regardless of child completion order; the prior-records
        snapshot is read once per wave and shared by every cycle in it.
    """

    @workflow.run
    async def run(self, batch_input: Dict[str, Any]) -> Dict[str, Any]:
        run_id = batch_input["run_id"]
        config_dict = batch_input["config"]
        batch_size = int(batch_input["batch_size"])
        batch_count = int(batch_input["batch_count"])
        max_parallel = max(1, int(batch_input["max_parallel"]))
        benchmark_symbol = batch_input.get("benchmark_symbol") or "SPY"
        exclude_asset_classes = batch_input.get("exclude_asset_classes")
        paper_trading_enabled = batch_input.get("paper_trading_enabled", True)
        paper_trading_lookback_days = batch_input.get("paper_trading_lookback_days", 365)
        start_cycle_offset = int(batch_input.get("start_cycle_offset", 0))
        # Fencing generation for this incarnation (minted by restart_strategy_lab_run
        # on a restart, carried forward unchanged by resume, defaulting to 1 for a
        # fresh run) — threaded into every persist/finalize activity call below so a
        # stale activity from a since-superseded incarnation is rejected instead of
        # silently committing (shared.fencing.check_fencing_token).
        generation = int(batch_input.get("generation", _DEFAULT_FENCING_GENERATION))

        wf_config = batch_input.get("workflow_config")
        if wf_config is None:
            wf_config = await _exec(act.resolve_workflow_config_activity)

        # Batch-level convergence tracker (aggregated across every cycle). Seeded
        # from the input for resume; snapshotted per cycle and merged back
        # sorted-by-cycle-index after each wave.
        primary_tracker_state: Dict[str, Any] = batch_input.get("convergence_tracker_state") or {}
        completed_record_ids: List[str] = list(batch_input.get("completed_record_ids") or [])
        completed_indices: set[int] = set(range(start_cycle_offset))
        errored = 0
        skipped = int(batch_input.get("skipped_cycles", 0))
        errored_details: List[Dict[str, Any]] = list(batch_input.get("errored_details") or [])
        tracker_merge_errors = int(batch_input.get("tracker_merge_error_count", 0))
        # The exact persisted external stop status ("cancelled"/"failed"/
        # "interrupted") observed between waves, or None if the run finished on
        # its own. Persisted verbatim as the terminal status so an external
        # interrupt/failure is never mislabeled a user cancellation (matching
        # thread mode's _strategy_lab_worker).
        external_terminal_status: Optional[str] = None
        # Resume: derive the starting batch + within-batch index from the flat offset.
        start_batch_idx, start_within_batch = divmod(start_cycle_offset, batch_size)
        # How many batches have fully completed so far, for the terminal SSE
        # event's informational completed_batches/total_batches fields only
        # (not itself persisted run state -- persist_run_state_activity's own
        # "completed_batches" write below is the source of truth for resume).
        # Approximated forward from the resume offset, matching
        # completed_indices' own resume-seed approximation just above.
        completed_batches_count = start_batch_idx

        # Gates the three new-in-this-change SSE publish call sites below
        # (cycle_skipped / cycle_complete / terminal). Called once, near the
        # top of run() before any activity/child-workflow command, so any run
        # already in flight when this ships (which by definition already
        # executed past this point under the old code) replays with this
        # False and simply doesn't publish for the remainder of its lifetime
        # -- the existing, already-safe degraded behavior (no live SSE
        # events), not a replay non-determinism error. Mirrors
        # ai_systems_team/temporal/workflows.py's established use of
        # workflow.patched for this exact purpose (see its own module
        # docstring / README for the pattern).
        sse_events_enabled = workflow.patched("strategy-lab-sse-run-events")

        for batch_idx in range(start_batch_idx, batch_count):
            within_start = start_within_batch if batch_idx == start_batch_idx else 0

            await self._persist_state(run_id, {"current_batch": batch_idx + 1}, generation)

            # ── Per-batch signal-brief refresh (batch N sees batches 1..N-1) ──
            brief = await _exec(
                act.compute_signal_brief_activity,
                params=benchmark_symbol,
                timeout=_ACTIVITY_TIMEOUT,
            )
            signal_brief = brief.get("signal_brief")
            signal_brief_storage = brief.get("signal_brief_storage")

            batch_start_cycle = batch_idx * batch_size
            remaining = list(
                range(batch_start_cycle + within_start, batch_start_cycle + batch_size)
            )

            while remaining:
                wave_indices = remaining[:max_parallel]
                remaining = remaining[max_parallel:]

                # Prior records read once per wave, shared by every cycle in it.
                prior_records = await _exec(act.snapshot_prior_records_activity)

                # Start every cycle in the wave as a child workflow BEFORE awaiting
                # any of them — this is what reproduces per-wave concurrency.
                handles: List[tuple[int, Any]] = []
                for cycle_index in wave_indices:
                    cycle_input = {
                        "run_id": run_id,
                        "generation": generation,
                        "cycle_index": cycle_index,
                        "prior_records": prior_records,
                        "config": config_dict,
                        "signal_brief": signal_brief,
                        "exclude_asset_classes": exclude_asset_classes,
                        "convergence_tracker_state": _snapshot_tracker_wire(primary_tracker_state),
                        "workflow_config": wf_config,
                        # Deterministic per-batch key (a string — safe to build in
                        # the workflow sandbox). Every cycle of this batch carries
                        # the same key, so when the batch-indicator-cache flag is
                        # on the worker resolves one shared BatchIndicatorCache per
                        # batch from it (see run_design_attempt_activity). Inert
                        # payload when the flag is off.
                        "batch_cache_key": f"{run_id}-b{batch_idx}",
                    }
                    handle = await workflow.start_child_workflow(
                        StrategyLabCycleWorkflow.run,
                        cycle_input,
                        id=f"{run_id}-c{cycle_index}",
                        task_queue=TASK_QUEUE,
                        retry_policy=_CHILD_RETRY,
                        execution_timeout=_CHILD_EXECUTION_TIMEOUT,
                    )
                    handles.append((cycle_index, handle))

                settled = await asyncio.gather(
                    *(handle for _, handle in handles), return_exceptions=True
                )

                # Finalize (paper-trade + persist) each settled cycle, then merge
                # the whole wave into the batch tracker in cycle-index order.
                wave_results: List[Dict[str, Any]] = []
                for (cycle_index, _), result in zip(handles, settled):
                    if isinstance(result, BaseException):
                        errored += 1
                        if len(errored_details) < _ERRORED_DETAILS_MAX:
                            # Walk the full Temporal cause chain to the terminal
                            # failure — a child-workflow failure surfaces here as
                            # ChildWorkflowError -> ActivityError -> ApplicationError
                            # (the domain error _map_exception_to_application_error
                            # produced), so a single ``__cause__`` hop only reaches
                            # ActivityError's generic RPC-boundary type/message, not
                            # the real failure. Follows explicit ``__cause__``
                            # chaining first and falls back to implicit
                            # ``__context__`` chaining (unless suppressed by
                            # ``raise ... from None``) once ``__cause__`` is
                            # exhausted — mirroring Python's own traceback
                            # resolution rule. Broader than ``_root_cause_message``
                            # in ``market_research_team/temporal/workflows.py``,
                            # which only follows ``__cause__``. Does not unwrap
                            # ``BaseExceptionGroup`` sub-exceptions.
                            underlying: BaseException = result
                            while True:
                                if underlying.__cause__ is not None:
                                    underlying = underlying.__cause__
                                elif (
                                    underlying.__context__ is not None
                                    and not underlying.__suppress_context__
                                ):
                                    underlying = underlying.__context__
                                else:
                                    break
                            # The terminal cause is usually the ApplicationError
                            # _map_exception_to_application_error produced, whose
                            # actionable classification lives in ``.type`` (e.g.
                            # "ValueError" or an LLM outcome like "fatal") — the
                            # Python class name itself is just "ApplicationError"
                            # for every such failure and would defeat the point
                            # of walking the chain.
                            if isinstance(underlying, ApplicationError) and underlying.type:
                                exception_type = underlying.type
                            else:
                                exception_type = type(underlying).__name__
                            errored_details.append(
                                {
                                    "cycle_index": cycle_index + 1,
                                    "batch_index": batch_idx + 1,
                                    "error": str(underlying),
                                    "exception_type": exception_type,
                                }
                            )
                        continue
                    if result.get("kind") == "skipped":
                        # No market data — a soft skip, not an error; the
                        # cycle contributes no record, so it's neither
                        # finalized nor merged into the batch tracker.
                        skipped += 1
                        if sse_events_enabled:
                            await _exec(
                                act.publish_run_event_activity,
                                params={
                                    "run_id": run_id,
                                    "event": {
                                        "type": "cycle_skipped",
                                        "cycle_index": cycle_index,
                                        "reason": "no_market_data",
                                        "batch_index": batch_idx + 1,
                                    },
                                },
                            )
                        continue
                    finalized = await _exec(
                        act.finalize_cycle_record_activity,
                        params={
                            "run_id": run_id,
                            "generation": generation,
                            "record": result["record"],
                            "signal_brief_storage": signal_brief_storage,
                            "paper_trading_enabled": paper_trading_enabled,
                            "paper_trading_lookback_days": paper_trading_lookback_days,
                        },
                    )
                    final_record = finalized["record"]
                    record_id = final_record.get("lab_record_id")
                    if record_id is not None:
                        completed_record_ids.append(record_id)
                    completed_indices.add(cycle_index)
                    if sse_events_enabled:
                        await _exec(
                            act.publish_run_event_activity,
                            params={
                                "run_id": run_id,
                                "event": {
                                    "type": "cycle_complete",
                                    "cycle_index": cycle_index,
                                    "record_id": record_id,
                                    "completed_cycles": len(completed_indices),
                                    "batch_index": batch_idx + 1,
                                },
                            },
                        )
                    wave_results.append(
                        {
                            "cycle_index": cycle_index,
                            "record": final_record,
                            "cycle_tracker_state": result["convergence_tracker_state"],
                        }
                    )

                if wave_results:
                    merged = await _exec(
                        act.merge_wave_results_activity,
                        params={
                            "primary_tracker_state": primary_tracker_state,
                            "wave_results": wave_results,
                        },
                    )
                    primary_tracker_state = merged["primary_tracker_state"]
                    for merge_error in merged.get("merge_errors") or []:
                        errored += 1
                        tracker_merge_errors += 1
                        if len(errored_details) < _ERRORED_DETAILS_MAX:
                            errored_details.append({**merge_error, "batch_index": batch_idx + 1})

                await self._persist_state(
                    run_id,
                    {
                        "completed_cycles": len(completed_indices),
                        "contiguous_cycles": _contiguous_prefix(completed_indices),
                        "completed_record_ids": list(completed_record_ids),
                        "errored_cycles": errored,
                        "skipped_cycles": skipped,
                        "errored_details": errored_details,
                        "tracker_merge_error_count": tracker_merge_errors,
                    },
                    generation,
                )

                # External stop is checked only between waves, mirroring thread
                # mode. Capture the TRUE status (cancelled/failed/interrupted),
                # not just a boolean, so it is persisted as-is below.
                external_terminal_status = await _exec(
                    act.external_terminal_status_activity, params=run_id
                )
                if external_terminal_status is not None:
                    break

            if external_terminal_status is not None:
                break
            await self._persist_state(run_id, {"completed_batches": batch_idx + 1}, generation)
            completed_batches_count = batch_idx + 1

        status = external_terminal_status or ("completed_with_errors" if errored else "completed")
        await self._persist_state(run_id, {"status": status}, generation)
        if sse_events_enabled:
            terminal_event = _terminal_sse_event(
                status=status,
                completed_count=len(completed_record_ids),
                skipped_count=skipped,
                errored_count=errored,
                errored_details=errored_details,
                completed_batches=completed_batches_count,
                total_batches=batch_count,
            )
            await _exec(
                act.publish_run_event_activity,
                params={"run_id": run_id, "event": terminal_event},
            )
        return {
            "run_id": run_id,
            "status": status,
            "completed_record_ids": completed_record_ids,
            "errored_cycles": errored,
            "skipped_cycles": skipped,
            "errored_details": errored_details,
            "tracker_merge_error_count": tracker_merge_errors,
            "convergence_tracker_state": primary_tracker_state,
        }

    async def _persist_state(self, run_id: str, state: Dict[str, Any], generation: int) -> None:
        """Persist a run-state delta via ``persist_run_state_activity``.

        ``persist_run_state_activity`` takes ``(run_id, state, create, generation)`` —
        four positional args — so it can't go through :func:`_exec` (single-``params``);
        call ``workflow.execute_activity`` directly with the same retry/timeout.

        Preconditions:
            - ``run_id`` is a non-empty string identifying an existing run.
            - ``state`` is a JSON-serializable dict of run-state deltas.
            - ``generation`` is a non-negative int (this workflow's fencing token).

        Raises when ``persist_run_state_activity`` rejects ``generation`` as stale
        (a non-retryable ``ApplicationError`` — a fenced write means this incarnation
        has been superseded by a restart and this workflow should stop, so letting the
        error propagate and fail the workflow is correct, not a bug to swallow). Apart
        from that stale-generation case, the underlying helper no longer swallows
        job-service failures either: a transient error is retried per
        ``_ACTIVITY_RETRY`` (2 attempts), and if that's exhausted this call — and
        thus the workflow — fails rather than silently continuing with a run
        state that never durably persisted. ``workflow.execute_activity`` can
        also propagate infrastructure-level failures (retry-policy exhaustion,
        worker unavailability, cancellation) unrelated to the activity's own
        business logic; those still propagate per Temporal's normal
        retry/timeout handling too.
        """
        create = False  # this call always updates an existing run's state, never creates one
        await workflow.execute_activity(
            act.persist_run_state_activity,
            args=[run_id, state, create, generation],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_ACTIVITY_RETRY,
        )


def _contiguous_prefix(completed_indices: "set[int]") -> int:
    """Return the count of the longest 0-based contiguous prefix of completed cycles.

    Mirrors thread-mode's ``contiguous_cycles`` resume anchor: the highest ``n``
    such that cycles ``0..n-1`` are all complete.

    Postconditions:
        Returns ``0`` when cycle ``0`` is incomplete; otherwise the length of the
        gap-free prefix starting at ``0``.
    """
    n = 0
    while n in completed_indices:
        n += 1
    return n


WORKFLOWS = [StrategyLabCycleWorkflow, StrategyLabBatchWorkflow]
ACTIVITIES = act.ACTIVITIES
