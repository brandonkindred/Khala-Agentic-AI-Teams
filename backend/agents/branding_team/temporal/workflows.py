"""Durable per-phase branding workflow.

``BrandingWorkflow`` reproduces the 5-phase branding pipeline as a durable,
resumable computation. It orchestrates the run as a sequence of activities —
begin → phase 1..N (each threading the prior phases' outputs forward) → optional
market-research/design-assets integrations → finalize — so a worker restart
re-runs only the unfinished activity instead of the whole ~2-hour pipeline.

The verdict is behavior-equivalent to thread mode because the assembly tail
(compliance + ``TeamOutput`` construction + persistence) runs through the same
``orchestrator._assemble_team_output`` the thread path uses; only the per-phase
LLM *inputs* differ (an isolated phase sees upstream outputs injected into its
task string rather than delivered through a Strands edge — a context superset).

Sandbox note: activity and constant imports are wrapped in
``workflow.unsafe.imports_passed_through()``; the workflow body performs no I/O,
time, or randomness — only ``execute_activity`` calls, ``asyncio.gather`` over
them, iteration over the constant ``PHASE_SEQUENCE``, and dict aggregation over
JSON-native values. Job-store I/O (checkpoints, status, cancel) lives entirely in
the activities.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any, Dict, List

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from branding_team.temporal import activities as _activities
    from branding_team.temporal.constants import PHASE_SEQUENCE, TASK_QUEUE

# Per-phase graph runs make many LLM calls; llm_service already fails over on
# transient provider errors, so one bounded retry is enough here — and because a
# retry only re-runs a single phase (and the phase activity's checkpoint short-
# circuits an already-produced output), it never re-runs the whole pipeline the
# way the old single-activity NO_RETRY design guarded against.
_LLM_RETRY = RetryPolicy(
    maximum_attempts=2,
    initial_interval=timedelta(seconds=20),
    maximum_interval=timedelta(minutes=3),
    backoff_coefficient=2.0,
)

# Cheap/deterministic bookkeeping + the design-assets stub: a slightly deeper
# bounded retry is safe because these are idempotent (finalize's brand-version
# append is checkpoint-gated).
_DEFAULT_RETRY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=10),
    maximum_interval=timedelta(minutes=1),
    backoff_coefficient=2.0,
)

_SHORT_TIMEOUT = timedelta(minutes=5)
_PHASE_TIMEOUT = timedelta(minutes=30)
_PHASE_HEARTBEAT_TIMEOUT = timedelta(minutes=5)
_MARKET_RESEARCH_TIMEOUT = timedelta(minutes=30)
_DESIGN_ASSETS_TIMEOUT = timedelta(minutes=10)
_FINALIZE_TIMEOUT = timedelta(minutes=10)


@workflow.defn(name="BrandingWorkflow")
class BrandingWorkflow:
    """Runs one branding job as a durable sequence of per-phase activities.

    Invariants:
        - Job-store status bookkeeping (RUNNING → COMPLETED/FAILED, cancel guards,
          checkpoints) is owned by the activities, never the workflow body.
        - ``prior_outputs`` is accumulated from activity return values (replayed
          deterministically from history on restart), never read back via a
          checkpoint inside the workflow body (which would be illegal I/O).
    """

    def __init__(self) -> None:
        # Progress is exposed via the ``progress`` query and cancellation via the
        # ``cancel`` signal; neither is required for a run to complete.
        self._phase: str = "starting"
        self._fraction: float = 0.0
        self._cancel_requested: bool = False

    @workflow.signal
    def cancel(self) -> None:
        """Request cooperative cancellation of the run.

        The flag is checked between phases, so a cancel that arrives mid-run
        short-circuits before the next (expensive) phase activity is dispatched.
        """
        self._cancel_requested = True

    @workflow.query
    def progress(self) -> Dict[str, Any]:
        """Return the current ``{phase, fraction, cancel_requested}`` snapshot."""
        return {
            "phase": self._phase,
            "fraction": self._fraction,
            "cancel_requested": self._cancel_requested,
        }

    def _advance(self, phase: str, fraction: float) -> None:
        self._phase = phase
        self._fraction = fraction

    @workflow.run
    async def run(self, payload: dict[str, Any]) -> None:
        """Durable entrypoint: run the branding pipeline for ``payload``.

        Preconditions:
            - ``payload`` is the serialized job dict built by ``_submit_brand_run``
              (``job_id`` + serialized mission/human_review/brand_checks +
              client/brand ids + integration flags + optional ``target_phase``).
            - ``payload['job_id']`` refers to a job already created in the store.
        Postconditions:
            - Drives the per-phase activities to completion; the finalize activity
              owns the COMPLETED transition. A cancel short-circuits (leaving the
              row cancelled, not failed). Any phase/finalize failure records a
              FAILED row and re-raises so the workflow reflects the failure.
        """
        job_id = payload["job_id"]
        target_phase = payload.get("target_phase")
        stop_idx = PHASE_SEQUENCE.index(target_phase) if target_phase else len(PHASE_SEQUENCE) - 1
        phases: List[str] = PHASE_SEQUENCE[: stop_idx + 1]

        self._advance("starting", 0.02)
        proceed = await workflow.execute_activity(
            _activities.begin_branding_job_activity,
            args=[job_id],
            task_queue=TASK_QUEUE,
            start_to_close_timeout=_SHORT_TIMEOUT,
            retry_policy=_DEFAULT_RETRY,
        )
        if not proceed:  # already cancelled at entry — terminal, not a failure
            self._advance("cancelled", 1.0)
            return

        try:
            prior_outputs: Dict[str, Any] = {}
            phase_count = len(phases) or 1
            for i, phase in enumerate(phases):
                if await self._cancelled(job_id):
                    self._advance("cancelled", self._fraction)
                    return
                self._advance(phase, 0.05 + 0.80 * (i / phase_count))
                out = await workflow.execute_activity(
                    _activities.run_branding_phase_activity,
                    args=[payload, phase, prior_outputs],
                    task_queue=TASK_QUEUE,
                    start_to_close_timeout=_PHASE_TIMEOUT,
                    heartbeat_timeout=_PHASE_HEARTBEAT_TIMEOUT,
                    retry_policy=_LLM_RETRY,
                )
                prior_outputs[phase] = out

            self._advance("integrations", 0.88)
            competitive_snapshot, design_asset_result = await self._run_integrations(
                payload, prior_outputs
            )

            self._advance("finalizing", 0.96)
            await workflow.execute_activity(
                _activities.finalize_branding_activity,
                args=[payload, prior_outputs, competitive_snapshot, design_asset_result],
                task_queue=TASK_QUEUE,
                start_to_close_timeout=_FINALIZE_TIMEOUT,
                retry_policy=_DEFAULT_RETRY,
            )
            self._advance("done", 1.0)
        except Exception as exc:  # noqa: BLE001 — record the failure, then re-raise
            # Mirror the except-branch of the old _run_branding_core: mark the job
            # FAILED (cancel-guarded inside the activity) and let the failure fail
            # the workflow, carrying the real cause.
            await workflow.execute_activity(
                _activities.mark_branding_failed_activity,
                args=[job_id, str(exc)],
                task_queue=TASK_QUEUE,
                start_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_DEFAULT_RETRY,
            )
            raise

    async def _cancelled(self, job_id: str) -> bool:
        """Return True if a cancel was signalled or the job row is cancelled.

        The signal flag is checked first (free); only if unset do we spend a
        job-service round-trip so an API-side cancel (which sets the row, not the
        signal) is still honored at the phase boundary.
        """
        if self._cancel_requested:
            return True
        return await workflow.execute_activity(
            _activities.check_branding_cancelled_activity,
            args=[job_id],
            task_queue=TASK_QUEUE,
            start_to_close_timeout=_SHORT_TIMEOUT,
            retry_policy=_DEFAULT_RETRY,
        )

    async def _run_integrations(
        self, payload: dict[str, Any], prior_outputs: Dict[str, Any]
    ) -> tuple[Any, Any]:
        """Run the enabled integrations concurrently; return ``(mr, da)`` results.

        Postconditions:
            - Returns ``(competitive_snapshot, design_asset_result)``; each is the
              activity's dict result, or ``None`` when its integration flag is off.
              Gathering enabled integrations concurrently mirrors thread mode's
              ``_gather_integrations``.
        """
        coros = []
        kinds: List[str] = []
        if payload.get("include_market_research"):
            coros.append(
                workflow.execute_activity(
                    _activities.run_market_research_activity,
                    args=[payload],
                    task_queue=TASK_QUEUE,
                    start_to_close_timeout=_MARKET_RESEARCH_TIMEOUT,
                    retry_policy=_LLM_RETRY,
                )
            )
            kinds.append("mr")
        if payload.get("include_design_assets"):
            coros.append(
                workflow.execute_activity(
                    _activities.run_design_assets_activity,
                    args=[payload, prior_outputs.get("strategic_core")],
                    task_queue=TASK_QUEUE,
                    start_to_close_timeout=_DESIGN_ASSETS_TIMEOUT,
                    retry_policy=_DEFAULT_RETRY,
                )
            )
            kinds.append("da")

        competitive_snapshot: Any = None
        design_asset_result: Any = None
        if coros:
            results = await asyncio.gather(*coros)
            for kind, result in zip(kinds, results):
                if kind == "mr":
                    competitive_snapshot = result
                else:
                    design_asset_result = result
        return competitive_snapshot, design_asset_result
