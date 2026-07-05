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

TASK_QUEUE = "accessibility_audit-queue"


@activity.defn(name="accessibility_audit_run_pipeline")
async def run_pipeline_activity(payload: dict[str, Any]) -> dict[str, Any]:
    """Run one accessibility-audit-create job to completion.

    Rebuilds the public request from ``payload`` and delegates to the same
    execution core the direct (non-Temporal) path uses, so job-store bookkeeping
    and the ``CreateAuditRequest -> AuditRequest`` conversion happen in one place.

    Preconditions:
        - ``payload`` has ``job_id``, ``audit_id``, and a ``request`` dict that
          validates as a ``CreateAuditRequest``.
    Postconditions:
        - The job's terminal state has been persisted to the shared job store;
          returns a small status dict for workflow observability.
    """
    # Import the side-effect-free execution core (NOT api.main), so running in a
    # worker-only process does not spin up the API's stale-job monitor / OTel.
    from accessibility_audit_team.audit_execution import CreateAuditRequest, execute_audit_job

    job_id = payload["job_id"]
    audit_id = payload["audit_id"]
    request = CreateAuditRequest(**payload["request"])
    await execute_audit_job(job_id, audit_id, request)
    return {"job_id": job_id, "audit_id": audit_id, "status": "done"}


@workflow.defn(name="AccessibilityAuditWorkflow")
class AccessibilityAuditWorkflow:
    @workflow.run
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Execute the audit pipeline activity for one audit-create job.

        Preconditions:
            - ``payload`` carries ``job_id``, ``audit_id`` and a ``request`` dict.
        Postconditions:
            - Returns the activity's status dict once the audit job has run to a
              terminal state (activity ``start_to_close_timeout`` is 2 hours).
        """
        return await workflow.execute_activity(
            run_pipeline_activity,
            payload,
            start_to_close_timeout=timedelta(hours=2),
        )


WORKFLOWS = [AccessibilityAuditWorkflow]
ACTIVITIES = [run_pipeline_activity]
