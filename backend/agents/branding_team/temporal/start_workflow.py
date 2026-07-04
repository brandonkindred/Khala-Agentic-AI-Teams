"""Start Branding Temporal workflows from synchronous API code.

Mirrors ``agent_provisioning_team/temporal/start_workflow.py``: bridges the
sync FastAPI handler to the async Temporal client that lives on the worker
thread's event loop. Fire-and-forget — it blocks only until the workflow start
is accepted (30s), never until the job completes.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from branding_team.temporal.constants import TASK_QUEUE, WORKFLOW_ID_PREFIX
from branding_team.temporal.workflows import BrandingWorkflow
from shared_temporal import get_temporal_client, get_temporal_loop

logger = logging.getLogger(__name__)

START_WORKFLOW_TIMEOUT = 30


def _run_async(coro: Any) -> Any:
    loop = get_temporal_loop()
    client = get_temporal_client()
    if loop is None or client is None:
        raise RuntimeError("Temporal client not available; is the Branding worker running?")
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=START_WORKFLOW_TIMEOUT)


def start_branding_workflow(job_id: str, payload: dict[str, Any]) -> None:
    """Start ``BrandingWorkflow`` for the given job.

    Preconditions:
        - ``job_id`` is a non-empty str whose job row already exists.
        - ``payload`` is JSON-serializable and carries the same ``job_id`` plus
          the serialized mission/human_review/etc. the activity reconstructs.
        - The Branding Temporal worker is running (Temporal enabled).
    Postconditions:
        - A ``BrandingWorkflow`` with id ``branding-{job_id}`` is started on the
          branding task queue. Raises ``RuntimeError`` if the Temporal client
          is unavailable.
    """
    client = get_temporal_client()
    if client is None:
        raise RuntimeError("Temporal client not available")
    workflow_id = f"{WORKFLOW_ID_PREFIX}{job_id}"
    _run_async(
        client.start_workflow(
            BrandingWorkflow.run,
            payload,
            id=workflow_id,
            task_queue=TASK_QUEUE,
        )
    )
    logger.info("Started BrandingWorkflow id=%s", workflow_id)
