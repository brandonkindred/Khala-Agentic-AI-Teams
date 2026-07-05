"""Start the accessibility-audit Temporal workflow from synchronous API code.

Thin per-team wrapper over ``shared_temporal.start_workflow_sync`` (the shared
sync->async bridge). The API owns its own job-store bookkeeping via
``JobServiceClient``, so we use ``start_workflow_sync`` (which does not touch the
job store) rather than ``run_team_job``.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def start_accessibility_audit_workflow(job_id: str, audit_id: str, request_payload: dict) -> str:
    """Dispatch ``AccessibilityAuditWorkflow`` for one audit-create job.

    Preconditions:
        - ``job_id`` and ``audit_id`` are non-empty identifiers, and a job row
          already exists for ``job_id``.
    Postconditions:
        - The accessibility_audit worker is ensured running in this process
          (idempotent), then a ``AccessibilityAuditWorkflow`` is started on
          ``TASK_QUEUE`` with id ``accessibility_audit-{job_id}``. Returns that
          ``workflow_id`` once Temporal accepts the workflow, so the caller can
          correlate it onto the job record.
    Raises:
        - ``ValueError`` if ``job_id``/``audit_id`` is blank (caller precondition).
        - ``RuntimeError`` if the worker's Temporal client never becomes available.
    """
    if not job_id:
        raise ValueError("job_id must be a non-empty job id")
    if not audit_id:
        raise ValueError("audit_id must be a non-empty audit id")

    from accessibility_audit_team.temporal import TASK_QUEUE, AccessibilityAuditWorkflow
    from accessibility_audit_team.temporal.worker import (
        start_accessibility_audit_temporal_worker_thread,
    )
    from shared_temporal import start_workflow_sync

    # Ensure a worker (and thus the shared client/loop) exists in THIS process,
    # regardless of how the app is served. Idempotent: a no-op when one already
    # runs (e.g. the docker team_service entrypoint already started it), and a
    # no-op returning False when Temporal is disabled.
    start_accessibility_audit_temporal_worker_thread()

    workflow_id = f"accessibility_audit-{job_id}"
    start_workflow_sync(
        AccessibilityAuditWorkflow.run,
        {"job_id": job_id, "audit_id": audit_id, "request": request_payload},
        workflow_id=workflow_id,
        task_queue=TASK_QUEUE,
    )
    logger.info("Started AccessibilityAuditWorkflow id=%s", workflow_id)
    return workflow_id
