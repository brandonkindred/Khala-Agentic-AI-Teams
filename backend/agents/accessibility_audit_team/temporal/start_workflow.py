"""Start the accessibility-audit Temporal workflow from synchronous API code.

Thin per-team wrapper over ``shared_temporal.start_workflow_sync`` (the shared
sync->async bridge). The API owns its own job-store bookkeeping via
``JobServiceClient``, so we use ``start_workflow_sync`` (which does not touch the
job store) rather than ``run_team_job``.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def start_accessibility_audit_workflow(job_id: str, audit_id: str, request_payload: dict) -> None:
    """Dispatch ``AccessibilityAuditWorkflow`` for one audit-create job.

    Preconditions:
        - ``job_id`` and ``audit_id`` are non-empty identifiers, and a job row
          already exists for ``job_id``.
        - The accessibility_audit Temporal worker is running (or starting) in this
          process, so the shared client/loop become available.
    Postconditions:
        - A ``AccessibilityAuditWorkflow`` is started on ``TASK_QUEUE`` with id
          ``accessibility_audit-{job_id}``; returns ``None`` once Temporal accepts it.
    Raises:
        - ``ValueError`` if ``job_id``/``audit_id`` is blank (caller precondition).
        - ``RuntimeError`` if the worker's Temporal client never becomes available.
    """
    if not job_id:
        raise ValueError("job_id must be a non-empty job id")
    if not audit_id:
        raise ValueError("audit_id must be a non-empty audit id")

    from accessibility_audit_team.temporal import TASK_QUEUE, AccessibilityAuditWorkflow
    from shared_temporal import start_workflow_sync

    workflow_id = f"accessibility_audit-{job_id}"
    start_workflow_sync(
        AccessibilityAuditWorkflow.run,
        {"job_id": job_id, "audit_id": audit_id, "request": request_payload},
        workflow_id=workflow_id,
        task_queue=TASK_QUEUE,
    )
    logger.info("Started AccessibilityAuditWorkflow id=%s", workflow_id)
