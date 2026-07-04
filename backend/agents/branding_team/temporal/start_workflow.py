"""Start the Branding Temporal workflow from synchronous API code.

Thin wrapper over ``shared_temporal.start_workflow_sync`` (the shared sync→async
bridge, which polls for the worker's Temporal client to become ready before
dispatching). We deliberately do NOT use ``shared_temporal.run_team_job`` here:
it creates its own job row (under the ``branding`` team slug) and sets
``status=running`` itself, which would collide with the API's ``create_job``
(namespaced under ``branding_team``) and the activity-owned RUNNING/COMPLETED
bookkeeping.
"""

from __future__ import annotations

import logging
from typing import Any

from branding_team.temporal.constants import TASK_QUEUE, WORKFLOW_ID_PREFIX
from branding_team.temporal.workflows import BrandingWorkflow
from shared_temporal import start_workflow_sync

logger = logging.getLogger(__name__)


def start_branding_workflow(job_id: str, payload: dict[str, Any]) -> None:
    """Start ``BrandingWorkflow`` for the given job.

    Preconditions:
        - ``job_id`` is a non-empty str whose job row already exists.
        - ``payload`` is JSON-serializable and carries the same ``job_id`` plus
          the serialized mission/human_review/etc. the activity reconstructs.
    Postconditions:
        - A workflow with id ``branding-<job_id>`` is started on the branding
          task queue (fire-and-forget; the caller polls
          ``GET /branding/status/{job_id}``). Raises ``RuntimeError`` if the
          worker's Temporal client never becomes available within the wait
          window.
    """
    workflow_id = f"{WORKFLOW_ID_PREFIX}{job_id}"
    start_workflow_sync(
        BrandingWorkflow.run,
        payload,
        workflow_id=workflow_id,
        task_queue=TASK_QUEUE,
    )
    logger.info("Started BrandingWorkflow id=%s", workflow_id)
