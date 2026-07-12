"""Start the SOC2 Temporal workflow from synchronous API code.

Thin wrapper over ``shared_temporal.start_workflow_sync`` (the shared sync→async
bridge). We deliberately do NOT use ``shared_temporal.run_team_job``: it creates
its own job row and sets ``status=running`` itself, which would collide with the
API's ``create_job`` and the activity-owned RUNNING/COMPLETED bookkeeping.
"""

from __future__ import annotations

import logging

from shared_temporal import start_workflow_sync
from soc2_compliance_team.temporal import (
    WORKFLOW_ID_PREFIX,
    Soc2AuditWorkflow,
    resolve_task_queue,
)

logger = logging.getLogger(__name__)


def start_audit_workflow(job_id: str, repo_path: str) -> None:
    """Start ``Soc2AuditWorkflow`` for the given job.

    Preconditions:
        - ``job_id`` is a job already created in the job store.
        - ``repo_path`` is a directory path on the worker host.

    Postconditions:
        - A workflow with id ``soc2-audit-<job_id>`` is started on the SOC2 task
          queue (raises ``RuntimeError`` if the worker client never becomes
          available within the wait window).
    """
    workflow_id = f"{WORKFLOW_ID_PREFIX}{job_id}"
    start_workflow_sync(
        Soc2AuditWorkflow.run,
        job_id,
        repo_path,
        workflow_id=workflow_id,
        task_queue=resolve_task_queue(),
    )
    logger.info("Started Soc2AuditWorkflow id=%s", workflow_id)
