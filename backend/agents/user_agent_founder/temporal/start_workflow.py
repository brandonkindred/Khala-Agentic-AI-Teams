"""Start the user_agent_founder Temporal workflow from synchronous API code.

Bridges a sync request handler into the Temporal worker's asyncio loop via
``asyncio.run_coroutine_threadsafe`` (mirrors ``blogging/temporal/start_workflow.py``).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from shared_temporal import get_temporal_client, get_temporal_loop
from user_agent_founder.temporal import (
    TASK_QUEUE,
    WORKFLOW_ID_PREFIX,
    UserAgentFounderWorkflow,
)

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
    accepts requests, but when the worker is started from the API
    lifespan (local dev) the daemon thread can lag behind the first
    request by tens of milliseconds. Polling here turns that race into a
    short wait instead of a 500.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        client = get_temporal_client()
        loop = get_temporal_loop()
        if client is not None and loop is not None:
            return client, loop
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "Temporal client not available; is the user_agent_founder worker running?"
            )
        time.sleep(CLIENT_READY_POLL_S)


def _run_async(coro: Any) -> Any:
    _, loop = _wait_for_client()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=START_WORKFLOW_TIMEOUT)


def start_founder_workflow(run_id: str) -> None:
    """Start ``UserAgentFounderWorkflow`` for the given run id."""
    client, _ = _wait_for_client()
    workflow_id = f"{WORKFLOW_ID_PREFIX}{run_id}"
    _run_async(
        client.start_workflow(
            UserAgentFounderWorkflow.run,
            run_id,
            id=workflow_id,
            task_queue=TASK_QUEUE,
        )
    )
    logger.info("Started UserAgentFounderWorkflow id=%s", workflow_id)
