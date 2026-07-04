"""Start the job matching Temporal workflow from synchronous API code.

Bridges a sync request handler into the Temporal worker's asyncio loop via
``asyncio.run_coroutine_threadsafe`` (mirrors
``user_agent_founder/temporal/start_workflow.py``).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from job_matching_team.temporal import (
    TASK_QUEUE,
    WORKFLOW_ID_PREFIX,
    JobMatchingWorkflow,
)
from shared_temporal import get_temporal_client, get_temporal_loop

logger = logging.getLogger(__name__)

START_WORKFLOW_TIMEOUT = 30
# How long to wait for the Temporal worker thread to connect its client before
# giving up. The worker connects in a daemon thread, so the very first request
# after a cold start can race the connect call.
CLIENT_READY_TIMEOUT_S = 10.0
CLIENT_READY_POLL_S = 0.05


def _wait_for_client(timeout_s: float = CLIENT_READY_TIMEOUT_S) -> tuple[Any, Any]:
    """Block briefly until the Temporal client + loop are populated.

    The team_service entrypoint normally starts the worker before uvicorn
    accepts requests, but when the worker is started from the API lifespan
    (local dev) the daemon thread can lag behind the first request by tens of
    milliseconds. Polling here turns that race into a short wait instead of a
    500.

    Preconditions:
        * ``timeout_s`` >= 0.
    Postconditions:
        * Returns a ``(client, loop)`` pair, both non-None, as soon as the worker
          thread has connected.
        * Raises ``RuntimeError`` if neither is populated within ``timeout_s``.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        client = get_temporal_client()
        loop = get_temporal_loop()
        if client is not None and loop is not None:
            return client, loop
        if time.monotonic() >= deadline:
            raise RuntimeError("Temporal client not available; is the job_matching worker running?")
        time.sleep(CLIENT_READY_POLL_S)


def _run_async(coro: Any) -> Any:
    """Run ``coro`` on the worker's event loop from this synchronous thread.

    Preconditions:
        * ``coro`` is an awaitable produced for the worker's loop (e.g. a
          ``client.start_workflow(...)`` coroutine).
    Postconditions:
        * Returns the coroutine's result, or re-raises its exception / a
          ``TimeoutError`` if it does not finish within ``START_WORKFLOW_TIMEOUT``.
    """
    _, loop = _wait_for_client()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=START_WORKFLOW_TIMEOUT)


def start_job_matching_workflow(job_id: str, request: dict[str, Any]) -> None:
    """Start ``JobMatchingWorkflow`` for the given scan job.

    Preconditions:
        * ``job_id`` refers to a job row already created by ``POST /scan``.
        * ``request`` is the JSON dump of a :class:`JobMatchRequest`.
    Postconditions:
        * A workflow with id ``job-matching-{job_id}`` is started on the team's
          task queue (idempotent — the deterministic id rejects duplicates).
    """
    client, _ = _wait_for_client()
    workflow_id = f"{WORKFLOW_ID_PREFIX}{job_id}"
    _run_async(
        client.start_workflow(  # pragma: no cover - requires a live Temporal server
            JobMatchingWorkflow.run,
            args=[job_id, request],
            id=workflow_id,
            task_queue=TASK_QUEUE,
        )
    )
    logger.info("Started JobMatchingWorkflow id=%s", workflow_id)
