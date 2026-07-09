"""Temporal workflow for the blogging team.

``BlogFullPipelineWorkflow`` orchestrates the blog pipeline as four sequential
activities — planning -> draft -> gates -> finalize — threading each phase's
serialized DTO into the next. Each phase retries independently under
``DEFAULT_RETRY_POLICY`` and shows up as a distinct span in the Temporal UI.

The planning and draft stages block on human input (outline approval, draft
review, title selection) via job-store polling inside their activities, so those
phases carry a generous ``schedule_to_close_timeout`` plus a heartbeat timeout (the
activity heartbeats every 30s while waiting). Unbounded, multi-day human waits would
require workflow-level signals; today's behavior is preserved.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from blogging.temporal import activities as _activities
    from blogging.temporal.constants import TASK_QUEUE

# HITL-bearing phases may wait on a human for hours; keep a wide ceiling (>= the
# former whole-pipeline 12h) so Temporal does not time the activity out mid-wait.
HITL_STAGE_TIMEOUT = timedelta(hours=12)
GATES_STAGE_TIMEOUT = timedelta(hours=12)
FINALIZE_TIMEOUT = timedelta(minutes=10)

DEFAULT_RETRY_POLICY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=30),
    maximum_interval=timedelta(minutes=2),
    backoff_coefficient=2.0,
)


@workflow.defn(name="BlogFullPipelineWorkflow")
class BlogFullPipelineWorkflow:
    """Runs the blog pipeline as planning -> draft -> gates -> finalize activities."""

    @workflow.run
    async def run(self, job_id: str, request_dict: Dict[str, Any]) -> None:
        """Execute the four pipeline-phase activities in sequence.

        Preconditions:
            - ``job_id`` identifies a created job record; ``request_dict`` is a
              serialized full-pipeline request.
        Postconditions:
            - On success each phase runs once and the finalize activity completes the
              job store. A ``FAIL`` status from any stage (cancelled/failed job)
              short-circuits without finalizing — the job store is already terminal.
              Histories recorded before the per-phase decomposition replay the
              original single-activity path (via ``workflow.patched``) so in-flight
              runs survive the deploy.
        """
        if not workflow.patched("blog-per-phase-activities"):
            # Drain-out branch: replays of pre-decomposition histories must
            # re-schedule the original monolithic activity deterministically.
            await workflow.execute_activity(
                _activities.run_full_pipeline_activity,
                args=[job_id, request_dict],
                task_queue=TASK_QUEUE,
                schedule_to_close_timeout=HITL_STAGE_TIMEOUT,
                heartbeat_timeout=timedelta(minutes=5),
                retry_policy=DEFAULT_RETRY_POLICY,
            )
            return

        planning = await workflow.execute_activity(
            _activities.plan_stage_activity,
            args=[job_id, request_dict],
            task_queue=TASK_QUEUE,
            schedule_to_close_timeout=HITL_STAGE_TIMEOUT,
            heartbeat_timeout=timedelta(minutes=5),
            retry_policy=DEFAULT_RETRY_POLICY,
        )
        if planning.get("status") != "PASS":
            return

        draft = await workflow.execute_activity(
            _activities.draft_stage_activity,
            args=[job_id, request_dict, planning],
            task_queue=TASK_QUEUE,
            schedule_to_close_timeout=HITL_STAGE_TIMEOUT,
            heartbeat_timeout=timedelta(minutes=5),
            retry_policy=DEFAULT_RETRY_POLICY,
        )
        if draft.get("status") != "PASS":
            return

        gates = await workflow.execute_activity(
            _activities.gates_stage_activity,
            args=[job_id, request_dict, planning, draft],
            task_queue=TASK_QUEUE,
            schedule_to_close_timeout=GATES_STAGE_TIMEOUT,
            heartbeat_timeout=timedelta(minutes=5),
            retry_policy=DEFAULT_RETRY_POLICY,
        )
        # "FAIL" means the job was cancelled/failed mid-stage (already terminal,
        # no final draft) — skip finalize. NEEDS_HUMAN_REVIEW still finalizes.
        if gates.get("status") == "FAIL":
            return

        await workflow.execute_activity(
            _activities.finalize_job_activity,
            args=[job_id, planning, gates],
            task_queue=TASK_QUEUE,
            schedule_to_close_timeout=FINALIZE_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )
