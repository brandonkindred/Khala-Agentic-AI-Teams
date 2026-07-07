"""Start the startup-advisor Temporal workflow from synchronous API code.

Thin wrapper over ``shared_temporal.start_workflow_sync`` (the shared sync→async
bridge). We deliberately do NOT use ``shared_temporal.run_team_job`` here: it
creates its own job row and sets ``status=running`` itself, which would
collide with the API's ``create_job`` and the activity-owned
RUNNING/COMPLETED bookkeeping.
"""

from __future__ import annotations

import logging

from shared_temporal import start_workflow_sync
from startup_advisor.temporal.constants import TASK_QUEUE, WORKFLOW_ID_PREFIX
from startup_advisor.temporal.workflows import StartupAdvisorWorkflow

logger = logging.getLogger(__name__)


def start_startup_advisor_workflow(job_id: str, message: str) -> None:
    """Start ``StartupAdvisorWorkflow`` for the given job.

    Preconditions:
        - ``job_id`` is a job already created in the job store.
        - ``message`` is the user's chat message.

    Postconditions:
        - A workflow with id ``startup-advisor-<job_id>`` is started on the
          startup-advisor task queue (raises ``RuntimeError`` if the worker
          client never becomes available within the wait window).
    """
    workflow_id = f"{WORKFLOW_ID_PREFIX}{job_id}"
    start_workflow_sync(
        StartupAdvisorWorkflow.run,
        job_id,
        message,
        workflow_id=workflow_id,
        task_queue=TASK_QUEUE,
    )
    logger.info("Started StartupAdvisorWorkflow id=%s", workflow_id)
