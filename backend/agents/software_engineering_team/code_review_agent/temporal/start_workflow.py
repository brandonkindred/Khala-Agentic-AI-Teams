"""Start-and-await the code review workflow from synchronous code.

``CodeReviewAgent.run`` is a synchronous call that must return a
``CodeReviewOutput``, so — unlike the fire-and-forget
``shared_temporal.start_workflow_sync`` — this bridge *executes* the workflow and
blocks for its result on the worker's asyncio loop.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

from shared_temporal.runner import _await_client

from .config import TASK_QUEUE

logger = logging.getLogger(__name__)

# Ceiling on how long a synchronous caller waits for the whole durable review.
# Generous because the map fan-out may include long LLM calls; the workflow's own
# per-activity timeouts bound each phase.
EXECUTE_TIMEOUT_S = 6 * 3600


def execute_code_review_workflow_sync(
    review_input: Dict[str, Any],
    *,
    workflow_id: str,
    client_ready_timeout_s: float | None = None,
    execute_timeout_s: float = EXECUTE_TIMEOUT_S,
) -> Dict[str, Any]:
    """Execute ``CodeReviewWorkflow`` and return its ``CodeReviewOutput`` dict.

    Preconditions:
        - ``review_input`` is a ``CodeReviewInput.model_dump(mode="json")`` dict.
        - ``workflow_id`` is non-empty and stable for identical resubmissions
          (so a duplicate concurrent review reuses the running workflow).

    Postconditions:
        - Returns the workflow's result dict once it completes.

    Raises:
        - ``RuntimeError`` if the worker's Temporal client never becomes available
          within the wait window (the caller treats this as "Temporal
          unavailable" and falls back to in-process review).
        - ``temporalio.client.WorkflowFailureError`` if the workflow itself fails
          (the caller unwraps a ``CodeReviewUnavailableError`` marker from it).
    """
    # Imported here (not at module load) so the workflow class — which pulls in
    # temporalio — is only required on the Temporal path.
    from .workflows import CodeReviewWorkflow

    client, loop = _await_client(client_ready_timeout_s)
    coro = client.execute_workflow(
        CodeReviewWorkflow.run,
        review_input,
        id=workflow_id,
        task_queue=TASK_QUEUE,
    )
    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=execute_timeout_s)
