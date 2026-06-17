"""Start Planning V3 Temporal workflows from sync API."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from planning_v3_team.temporal.client import (
    get_temporal_client,
    get_temporal_loop,
)
from planning_v3_team.temporal.constants import TASK_QUEUE, WORKFLOW_ID_PREFIX
from planning_v3_team.temporal.workflows import PlanningV3Workflow

logger = logging.getLogger(__name__)

START_WORKFLOW_TIMEOUT = 30
# Cold-start race: the worker connects its client/loop in a daemon thread, so
# the first request after startup can arrive before those module globals are
# set. Poll briefly to turn that race into a short wait instead of a 500.
CLIENT_READY_TIMEOUT_S = 10.0
CLIENT_READY_POLL_S = 0.05


def _worker_starting() -> bool:
    """True while a Temporal worker thread is alive in this process.

    Imported lazily so this module doesn't pull ``temporalio.worker`` (via the
    worker module) at import time, and to keep the dependency direction
    one-way (worker → start_workflow never the reverse).

    Preconditions:
        - None.
    Postconditions:
        - Returns True while a Planning V3 worker thread exists and is alive in
          this process (connecting or connected); False when no worker is
          running here, so a bounded wait for the client would be futile.
    """
    from planning_v3_team.temporal.worker import is_worker_thread_alive

    return is_worker_thread_alive()


def _wait_for_client() -> tuple[Any, Any]:
    """Block until the Temporal client and loop are populated, or fail.

    Preconditions:
        - The Planning V3 Temporal worker has been (or is being) started in
          this process — normally by the team_service per-worker bootstrap or
          the API lifespan. The client/loop are stored in module-level globals
          that the worker thread fills once it connects.
    Postconditions:
        - Returns ``(client, loop)`` with both non-None once the worker thread
          has connected, within ``CLIENT_READY_TIMEOUT_S`` seconds. Both are
          required together so callers never observe a half-initialised state
          (the worker sets the client just before the loop).
    Raises:
        - ``RuntimeError`` immediately if no worker thread is running in this
          process (nothing to wait for), or after ``CLIENT_READY_TIMEOUT_S`` if
          a running worker never connects.
    """
    deadline = time.monotonic() + CLIENT_READY_TIMEOUT_S
    while True:
        client = get_temporal_client()
        loop = get_temporal_loop()
        if client is not None and loop is not None:
            return client, loop
        # Fail fast when no worker thread is running here: the bounded wait only
        # buys anything while a worker is mid-connect. If it is absent or has
        # died (e.g. Temporal unreachable, so the connect raised and the thread
        # exited), blocking the full timeout would tie up the request threadpool
        # for no benefit — the sync /run endpoint runs in a bounded thread pool.
        if not _worker_starting():
            raise RuntimeError(
                "Temporal client not available; the Planning V3 worker is not running"
            )
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"Temporal client not available after {CLIENT_READY_TIMEOUT_S:.0f}s; "
                "the Planning V3 worker is running but cannot reach Temporal"
            )
        time.sleep(CLIENT_READY_POLL_S)


def _run_async(coro: Any, loop: Any) -> Any:
    """Submit ``coro`` to the worker's event loop and block for the result.

    Preconditions:
        - ``loop`` is the running event loop owned by the Temporal worker
          thread (as returned by ``_wait_for_client``), and ``coro`` is a
          coroutine created from that worker's connected client.
    Postconditions:
        - Returns the coroutine's result, waiting up to
          ``START_WORKFLOW_TIMEOUT`` seconds for it to complete.
    """
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=START_WORKFLOW_TIMEOUT)


def start_planning_v3_workflow(
    job_id: str,
    repo_path: str,
    client_name: Optional[str],
    initial_brief: Optional[str],
    spec_content: Optional[str],
    use_product_analysis: bool,
    use_planning_v2: bool,
    use_market_research: bool,
) -> None:
    """Start PlanningV3Workflow for the given job.

    Preconditions:
        - ``job_id`` is a non-empty, unique run id and ``repo_path`` is a
          non-empty workspace path (both enforced below). The Planning V3
          Temporal worker is running (or starting) in this process.
    Postconditions:
        - A ``PlanningV3Workflow`` is started on ``TASK_QUEUE`` with id
          ``WORKFLOW_ID_PREFIX + job_id``; returns once Temporal accepts it.
    Raises:
        - ``ValueError`` if ``job_id`` or ``repo_path`` is blank (precondition
          violated by the caller).
        - ``RuntimeError`` if the Temporal client never becomes available
          within ``CLIENT_READY_TIMEOUT_S`` (worker not running / misconfigured).
        - Temporal client errors (e.g. ``temporalio.exceptions.TemporalError`` /
          ``WorkflowAlreadyStartedError``) or ``concurrent.futures.TimeoutError``
          propagated from ``client.start_workflow`` / ``_run_async`` if the
          worker loop does not accept the workflow within ``START_WORKFLOW_TIMEOUT``.
    """
    # Explicit checks (not asserts) so the precondition holds under ``python -O``.
    if not job_id:
        raise ValueError("job_id must be a non-empty run id")
    if not repo_path:
        raise ValueError("repo_path must be a non-empty workspace path")
    client, loop = _wait_for_client()
    workflow_id = f"{WORKFLOW_ID_PREFIX}{job_id}"
    _run_async(
        client.start_workflow(
            PlanningV3Workflow.run,
            args=[
                job_id,
                repo_path,
                client_name,
                initial_brief,
                spec_content,
                use_product_analysis,
                use_planning_v2,
                use_market_research,
            ],
            id=workflow_id,
            task_queue=TASK_QUEUE,
        ),
        loop,
    )
    logger.info("Started PlanningV3Workflow id=%s", workflow_id)
