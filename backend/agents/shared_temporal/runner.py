"""Shared workflow-dispatch helpers for teams' sync HTTP handlers.

Two entrypoints, both bridging a sync handler into the worker's asyncio loop:

- ``start_workflow_sync`` — waits for the worker's connected client + loop, then
  starts a workflow with a caller-supplied id/queue. Does NOT touch the job
  store; use it when the caller owns its own job-status bookkeeping.
- ``run_team_job`` — the job-store-backed flow: ensures a ``JobServiceClient``
  row exists, then dispatches via ``start_workflow_sync`` with a deterministic
  ``{team}-{job_id}`` id (so re-submits are idempotent) and marks it running.

Temporal is required. The system will fail fast if TEMPORAL_ADDRESS is not set.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from shared_temporal.client import (
    get_default_task_queue,
    get_temporal_client,
    get_temporal_loop,
    is_temporal_enabled,
)

logger = logging.getLogger(__name__)

# How long to wait for the worker thread to connect its client before giving
# up, and the sync-start dispatch timeout. The worker connects in a daemon
# thread, so the very first request after a cold start can race the connect.
CLIENT_READY_TIMEOUT_S = 10.0
CLIENT_READY_POLL_S = 0.05
START_WORKFLOW_TIMEOUT_S = 30


def _await_client(timeout_s: float | None = None) -> tuple[Any, Any]:
    """Block briefly until the shared Temporal client + loop are populated.

    The team_service entrypoint normally starts the worker before uvicorn
    accepts requests, but when the worker is started from an app lifespan
    (local dev) the daemon thread can lag the first request by tens of
    milliseconds. Polling turns that race into a short wait instead of a 500.

    Preconditions:
        - ``timeout_s`` is ``None`` (use ``CLIENT_READY_TIMEOUT_S``) or >= 0.

    Postconditions:
        - Returns ``(client, loop)`` once both are connected and the loop is
          open (running), or raises ``RuntimeError`` after ``timeout_s`` seconds
          if they never become available. A closed loop — left behind by a
          worker that connected and then died — counts as "not ready", so the
          poll never hands back a loop that ``run_coroutine_threadsafe`` would
          reject with "Event loop is closed".
    """
    # Resolve at call time (not as a bound default) so monkeypatching the
    # module constant in tests takes effect.
    if timeout_s is None:
        timeout_s = CLIENT_READY_TIMEOUT_S
    deadline = time.monotonic() + timeout_s
    while True:
        client = get_temporal_client()
        loop = get_temporal_loop()
        if client is not None and loop is not None and not loop.is_closed():
            return client, loop
        if time.monotonic() >= deadline:
            raise RuntimeError("Temporal client not available; is the team's worker running?")
        time.sleep(CLIENT_READY_POLL_S)


def start_workflow_sync(
    workflow_run: Any,
    *args: Any,
    workflow_id: str,
    task_queue: str,
    client_ready_timeout_s: float | None = None,
    start_timeout_s: float = START_WORKFLOW_TIMEOUT_S,
) -> None:
    """Start a Temporal workflow from synchronous code.

    The shared sync→async bridge every team's ``start_*_workflow`` helper wraps:
    wait (briefly, polling) for the worker's connected client + loop, then
    schedule ``client.start_workflow`` on the worker loop and block until it is
    accepted. Unlike :func:`run_team_job`, this does NOT touch the job store —
    callers that own their own job-status bookkeeping use this instead.

    Preconditions:
        - ``workflow_id`` and ``task_queue`` are non-empty.

    Postconditions:
        - The workflow is started (raises ``RuntimeError`` if the worker client
          never becomes available within ``client_ready_timeout_s``, defaulting
          to ``CLIENT_READY_TIMEOUT_S``).
    """
    client, loop = _await_client(client_ready_timeout_s)
    coro = client.start_workflow(
        workflow_run, args=list(args), id=workflow_id, task_queue=task_queue
    )
    asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=start_timeout_s)


def signal_workflow_sync(
    workflow_id: str,
    signal_name: str,
    *args: Any,
    client_ready_timeout_s: float | None = None,
    timeout_s: float = START_WORKFLOW_TIMEOUT_S,
) -> None:
    """Signal a running Temporal workflow from synchronous code.

    The signal companion to :func:`start_workflow_sync`: wait (briefly, polling)
    for the worker's connected client + loop, resolve the workflow handle by id,
    then schedule ``handle.signal`` on the worker loop and block until delivery is
    accepted. Does NOT touch the job store — the workflow's own signal handler +
    activities own any resulting state change.

    Preconditions:
        - ``workflow_id`` and ``signal_name`` are non-empty.
        - ``args`` are Temporal-serializable (the same codec the workflow uses).

    Postconditions:
        - The signal is delivered to the workflow with id ``workflow_id`` (raises
          ``RuntimeError`` if the worker client never becomes available within
          ``client_ready_timeout_s``, defaulting to ``CLIENT_READY_TIMEOUT_S``).
    """
    assert workflow_id, "workflow_id must be non-empty"
    assert signal_name, "signal_name must be non-empty"
    client, loop = _await_client(client_ready_timeout_s)
    handle = client.get_workflow_handle(workflow_id)
    asyncio.run_coroutine_threadsafe(handle.signal(signal_name, *args), loop).result(
        timeout=timeout_s
    )


def cancel_workflow_sync(
    workflow_id: str,
    *,
    client_ready_timeout_s: float | None = None,
    timeout_s: float = START_WORKFLOW_TIMEOUT_S,
) -> None:
    """Request cancellation of a running Temporal workflow from synchronous code.

    The cancel companion to :func:`start_workflow_sync`: wait (briefly, polling)
    for the worker's connected client + loop, resolve the workflow handle by id,
    then schedule ``handle.cancel`` on the worker loop and block until the request
    is accepted. The workflow observes an ``asyncio.CancelledError`` at its next
    await point; any store reconciliation is the workflow's responsibility.

    Preconditions:
        - ``workflow_id`` is non-empty.

    Postconditions:
        - Cancellation is requested for the workflow with id ``workflow_id``
          (raises ``RuntimeError`` if the worker client never becomes available
          within ``client_ready_timeout_s``, defaulting to
          ``CLIENT_READY_TIMEOUT_S``). Requesting cancel on an already-terminal
          workflow is a no-op accepted by the server.
    """
    assert workflow_id, "workflow_id must be non-empty"
    client, loop = _await_client(client_ready_timeout_s)
    handle = client.get_workflow_handle(workflow_id)
    asyncio.run_coroutine_threadsafe(handle.cancel(), loop).result(timeout=timeout_s)


def _get_job_manager(team: str) -> Any:
    # Import lazily (not at module top) on purpose: only ``run_team_job`` — the
    # job-store-backed dispatch path — needs ``JobServiceClient`` (and its transitive
    # ``httpx`` dependency). The lightweight bridges in this module
    # (``start_workflow_sync`` / ``signal_workflow_sync`` / ``cancel_workflow_sync``),
    # which many callers use without ever touching the job store, must not pull that in
    # just by importing ``shared_temporal.runner``. There is no circular-import concern
    # (``job_service_client`` does not import ``shared_temporal``); this is purely to
    # keep the import surface of the bridge path minimal.
    from job_service_client import JobServiceClient

    return JobServiceClient(team=team)


def run_team_job(
    team: str,
    job_id: str,
    workflow: Any,
    workflow_args: Optional[list[Any]] = None,
    *,
    task_queue: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Create/update a job record and dispatch the team's workflow.

    Args:
        team: Team slug (used for job store namespace and workflow ID prefix).
        job_id: Caller-supplied or generated job ID.
        workflow: Temporal workflow class or ``workflow.run`` reference.
        workflow_args: Positional args passed to the workflow run method.
        task_queue: Override team task queue (defaults to
            ``TEMPORAL_TASK_QUEUE`` env var).
        metadata: Extra fields to persist on the initial job record.

    Raises:
        RuntimeError: If Temporal is not enabled (TEMPORAL_ADDRESS not set).
    """
    if not is_temporal_enabled():
        raise RuntimeError(
            "Temporal is required but TEMPORAL_ADDRESS is not set. "
            "All agent teams require Temporal for durable workflow execution."
        )

    manager = _get_job_manager(team)
    init_fields = {"status": "pending"}
    if metadata:
        init_fields.update(metadata)
    manager.create_job(job_id, **init_fields)

    workflow_id = f"{team}-{job_id}"
    start_workflow_sync(
        workflow,
        *(workflow_args or []),
        workflow_id=workflow_id,
        task_queue=task_queue or get_default_task_queue(),
    )
    manager.update_job(job_id, status="running", workflow_id=workflow_id)
    return {"job_id": job_id, "team": team, "status": "running", "mode": "temporal"}
