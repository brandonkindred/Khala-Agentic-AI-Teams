"""Start the sales pipeline Temporal workflow from synchronous API code.

Thin wrapper over ``shared_temporal.start_workflow_sync`` (the shared sync→async
bridge). We deliberately do NOT use ``shared_temporal.run_team_job`` here: it
creates its own job row (under the ``sales`` team slug) and sets
``status=running`` itself, which would collide with the API's
``_job_manager.create_job`` (namespaced under ``sales_team``) and the
activity-owned RUNNING/COMPLETED/FAILED bookkeeping.
"""

from __future__ import annotations

import logging
from typing import Any

from sales_team.temporal import TASK_QUEUE, WORKFLOW_ID_PREFIX, SalesWorkflow
from shared_temporal import start_workflow_sync

logger = logging.getLogger(__name__)


def start_sales_workflow(job_id: str, request: dict[str, Any]) -> None:
    """Start ``SalesWorkflow`` for the given job.

    Preconditions:
        - ``job_id`` is a job already created in the job store.
        - ``request`` is the serialized ``SalesPipelineRequest``
          (``payload.model_dump(mode="json")``).

    Postconditions:
        - A workflow with id ``sales-<job_id>`` is started on the sales task
          queue (raises ``RuntimeError`` if the worker client never becomes
          available within the wait window).
    """
    workflow_id = f"{WORKFLOW_ID_PREFIX}{job_id}"
    start_workflow_sync(
        SalesWorkflow.run,
        job_id,
        request,
        workflow_id=workflow_id,
        task_queue=TASK_QUEUE,
    )
    logger.info("Started SalesWorkflow id=%s", workflow_id)
