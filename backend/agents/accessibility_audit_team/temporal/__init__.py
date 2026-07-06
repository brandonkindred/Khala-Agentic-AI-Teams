"""Temporal workflow + activity wrapping the accessibility audit orchestrator.

Importing this package is intentionally side-effect free: it must NOT start a
worker or call ``os.getenv``/``is_temporal_enabled`` at module top level. The
temporalio workflow sandbox re-imports this module to register the workflow and
aborts on restricted calls, and a self-bootstrapping worker races the first
dispatch. Worker boot lives in ``temporal.worker`` (invoked by the team_service
entrypoint); dispatch lives in ``temporal.start_workflow``.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

TASK_QUEUE = "accessibility_audit-queue"

# Bounded retry for the audit activity. An infrastructure failure inside the
# audit propagates out of ``run_audit_job`` (it is NOT swallowed on this path),
# so Temporal retries it a few times before failing the workflow. A logical
# failure (audit ran, target unauditable) is recorded on the job and returns
# normally, so it does not trigger a retry.
_AUDIT_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=3,
)


@activity.defn(name="accessibility_audit_run_pipeline")
async def run_pipeline_activity(payload: dict[str, Any]) -> dict[str, Any]:
    """Run one accessibility-audit-create job to completion.

    Rebuilds the public request from ``payload`` and delegates to the shared
    execution core. It calls ``run_audit_job`` (not ``execute_audit_job``) so an
    infrastructure failure propagates and fails the activity, letting Temporal's
    retry policy recover instead of silently recording a failed job under a green
    workflow.

    Preconditions:
        - ``payload`` has ``job_id``, ``audit_id``, and a ``request`` dict that
          validates as a ``CreateAuditRequest``.
    Postconditions:
        - The job's terminal state has been persisted to the shared job store and
          a small status dict returned; an infrastructure exception propagates
          (failing the activity) rather than being swallowed.
    """
    # Import the side-effect-free execution core (NOT api.main), so running in a
    # worker-only process does not spin up the API's stale-job monitor / OTel.
    from accessibility_audit_team.audit_execution import CreateAuditRequest, run_audit_job

    job_id = payload["job_id"]
    audit_id = payload["audit_id"]
    request = CreateAuditRequest(**payload["request"])
    await run_audit_job(job_id, audit_id, request)
    return {"job_id": job_id, "audit_id": audit_id, "status": "done"}


@workflow.defn(name="AccessibilityAuditWorkflow")
class AccessibilityAuditWorkflow:
    """Durable workflow wrapping a single accessibility-audit-create job.

    Invariants:
        - Owns exactly one ``run_pipeline_activity`` execution; the job's status
          transitions are written by that activity, not by the workflow.
    """

    @workflow.run
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Execute the audit pipeline activity for one audit-create job.

        Preconditions:
            - ``payload`` carries ``job_id``, ``audit_id`` and a ``request`` dict.
        Postconditions:
            - Returns the activity's status dict once the audit job has run to a
              terminal state (activity ``start_to_close_timeout`` is 2 hours,
              retried per ``_AUDIT_RETRY_POLICY`` on infrastructure failure).
        """
        return await workflow.execute_activity(
            run_pipeline_activity,
            payload,
            start_to_close_timeout=timedelta(hours=2),
            retry_policy=_AUDIT_RETRY_POLICY,
        )


WORKFLOWS = [AccessibilityAuditWorkflow]
ACTIVITIES = [run_pipeline_activity]
