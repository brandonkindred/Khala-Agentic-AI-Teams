"""Start the deepthought Temporal workflow from synchronous API code.

Thin wrapper over ``shared_temporal.start_workflow_sync`` (the shared sync→async
bridge), so the API handler owns its own job-store bookkeeping while the
orchestrator runs durably on the Temporal worker.
"""

from __future__ import annotations

import logging
from typing import Any

from deepthought.temporal import TASK_QUEUE, WORKFLOW_ID_PREFIX, DeepthoughtWorkflow
from shared_temporal import start_workflow_sync

logger = logging.getLogger(__name__)


def start_deepthought_workflow(job_id: str, request: dict[str, Any]) -> None:
    """Start ``DeepthoughtWorkflow`` for the given job.

    Preconditions:
        - ``job_id`` is non-empty and refers to a job row already created by
          the caller.
        - ``request`` is a ``DeepthoughtRequest.model_dump()`` payload.
        - Temporal is enabled and its worker is (or will shortly be) running.

    Postconditions:
        - The workflow is started with a deterministic id
          ``f"{WORKFLOW_ID_PREFIX}{job_id}"`` (idempotent re-submits); raises
          ``RuntimeError`` if the worker client never becomes available.
    """
    workflow_id = f"{WORKFLOW_ID_PREFIX}{job_id}"
    start_workflow_sync(
        DeepthoughtWorkflow.run,
        job_id,
        request,
        workflow_id=workflow_id,
        task_queue=TASK_QUEUE,
    )
    logger.info("Started DeepthoughtWorkflow id=%s", workflow_id)
