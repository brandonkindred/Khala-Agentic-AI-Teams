"""Start the job matching Temporal workflow from synchronous API code.

Thin wrapper over ``shared_temporal.start_workflow_sync`` (the shared sync→async
bridge that waits for the worker's connected client + loop, then schedules the
start on the worker loop). We deliberately do NOT use
``shared_temporal.run_team_job``: it creates its own job row and marks it
running, which would collide with the API's ``create_job`` and the
activity-owned RUNNING/COMPLETED bookkeeping.
"""

from __future__ import annotations

import logging
from typing import Any

from job_matching_team.temporal import (
    TASK_QUEUE,
    WORKFLOW_ID_PREFIX,
    JobMatchingWorkflow,
)
from shared_temporal import start_workflow_sync

logger = logging.getLogger(__name__)


def start_job_matching_workflow(job_id: str, request: dict[str, Any]) -> None:
    """Start ``JobMatchingWorkflow`` for the given scan job.

    Preconditions:
        * ``job_id`` refers to a job row already created by ``POST /scan``.
        * ``request`` is the JSON dump of a :class:`JobMatchRequest`.
    Postconditions:
        * A workflow with id ``job-matching-{job_id}`` is started on the team's
          task queue (the deterministic id rejects duplicate submissions).
        * Raises ``RuntimeError`` if the worker client never becomes available
          within the shared wait window — the shared helper rejects a loop left
          behind by a dead worker, so this never submits to a closed loop.
    """
    workflow_id = f"{WORKFLOW_ID_PREFIX}{job_id}"
    start_workflow_sync(
        JobMatchingWorkflow.run,
        job_id,
        request,
        workflow_id=workflow_id,
        task_queue=TASK_QUEUE,
    )
    logger.info("Started JobMatchingWorkflow id=%s", workflow_id)
