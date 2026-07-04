"""Start the market_research Temporal workflow from synchronous API code.

Bridges a sync request handler into the Temporal worker's asyncio loop via
``asyncio.run_coroutine_threadsafe`` (mirrors
``user_agent_founder/temporal/start_workflow.py``).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from market_research_team.temporal import (
    TASK_QUEUE,
    WORKFLOW_ID_PREFIX,
    MarketResearchWorkflow,
)
from shared_temporal import get_temporal_client, get_temporal_loop

logger = logging.getLogger(__name__)

START_WORKFLOW_TIMEOUT = 30
# How long to wait for the Temporal worker thread to connect its client
# before giving up. The worker connects in a daemon thread, so the very
# first request after a cold start can race the connect call.
CLIENT_READY_TIMEOUT_S = 10.0
CLIENT_READY_POLL_S = 0.05


def _wait_for_client(timeout_s: float = CLIENT_READY_TIMEOUT_S) -> tuple[Any, Any]:
    """Block briefly until the Temporal client + loop are populated.

    The team_service entrypoint normally starts the worker before uvicorn
    accepts requests, but when the worker is started from the API lifespan
    (local dev) the daemon thread can lag behind the first request by tens
    of milliseconds. Polling here turns that race into a short wait instead
    of a 500.

    Preconditions:
        - ``timeout_s`` >= 0.

    Postconditions:
        - Returns ``(client, loop)`` once both the shared Temporal client and
          its event loop are connected, or raises ``RuntimeError`` after
          ``timeout_s`` seconds if they never become available.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        client = get_temporal_client()
        loop = get_temporal_loop()
        if client is not None and loop is not None:
            return client, loop
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "Temporal client not available; is the market_research worker running?"
            )
        time.sleep(CLIENT_READY_POLL_S)


def _run_async(coro: Any, loop: Any) -> Any:
    """Run ``coro`` on the worker's event ``loop`` and block for the result.

    Preconditions:
        - ``loop`` is the running Temporal worker event loop
          (from ``_wait_for_client``).

    Postconditions:
        - Returns the coroutine result, or raises if it does not complete
          within ``START_WORKFLOW_TIMEOUT`` seconds.
    """
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=START_WORKFLOW_TIMEOUT)


def start_market_research_workflow(job_id: str, request: dict[str, Any]) -> None:
    """Start ``MarketResearchWorkflow`` for the given job.

    Preconditions:
        - ``job_id`` is a job already created in the job store.
        - ``request`` is the serialized ``RunMarketResearchRequest``
          (``payload.model_dump()``).

    Postconditions:
        - A workflow with id ``market-research-<job_id>`` is started on the
          market_research task queue (raises ``RuntimeError`` if the worker
          client never becomes available within the wait window).
    """
    client, loop = _wait_for_client()
    workflow_id = f"{WORKFLOW_ID_PREFIX}{job_id}"
    _run_async(
        client.start_workflow(
            MarketResearchWorkflow.run,
            args=[job_id, request],
            id=workflow_id,
            task_queue=TASK_QUEUE,
        ),
        loop,
    )
    logger.info("Started MarketResearchWorkflow id=%s", workflow_id)
