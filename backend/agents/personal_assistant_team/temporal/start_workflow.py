"""Start the personal-assistant Temporal workflow from synchronous API code.

Thin wrapper over ``shared.temporal.start_workflow_sync`` (the shared
sync->async bridge). We deliberately do NOT use ``shared.temporal.run_team_job``
here: it would create its own job row under the ``personal_assistant`` team
slug and set ``status=running`` itself, colliding with the API's ``create_job``
(namespaced under ``personal_assistant_team``) and the activity-owned
RUNNING/COMPLETED/FAILED bookkeeping.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from personal_assistant_team.temporal import (
    TASK_QUEUE,
    WORKFLOW_ID_PREFIX_ASSISTANT,
    PaAssistantWorkflow,
)
from shared.temporal import start_workflow_sync

logger = logging.getLogger(__name__)


def start_assistant_workflow(
    job_id: str,
    user_id: str,
    message: str,
    context: Optional[Dict[str, Any]] = None,
) -> None:
    """Start ``PaAssistantWorkflow`` for the given job.

    Preconditions:
        - ``job_id`` is a job already created in the PA job store.

    Postconditions:
        - A workflow with id ``pa-assistant-<job_id>`` is started on the
          personal-assistant task queue (raises ``RuntimeError`` if the worker
          client never becomes available within the wait window).
    """
    workflow_id = f"{WORKFLOW_ID_PREFIX_ASSISTANT}{job_id}"
    start_workflow_sync(
        PaAssistantWorkflow.run,
        job_id,
        user_id,
        message,
        context or {},
        workflow_id=workflow_id,
        task_queue=TASK_QUEUE,
    )
    logger.info("Started PaAssistantWorkflow id=%s", workflow_id)
