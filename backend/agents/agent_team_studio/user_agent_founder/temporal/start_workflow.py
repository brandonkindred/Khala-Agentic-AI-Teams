"""Drive the user_agent_founder Temporal workflow from synchronous API code.

Bridges a sync FastAPI request handler into the Temporal worker's asyncio loop
via ``asyncio.run_coroutine_threadsafe`` (mirrors
``blogging/temporal/start_workflow.py``).

Public API (called from ``api/main.py``):
    - :func:`start_founder_workflow` — dispatch a new ``UserAgentFounderWorkflow``
      for a run id; called by ``_dispatch_founder_run`` on ``/start`` (and
      ``/resume``/``/restart``) when Temporal is enabled.
    - :func:`cancel_founder_workflow` — deliver the cooperative ``cancel`` signal
      to an in-flight workflow; called (best-effort) from the ``POST
      /job/{id}/cancel`` route after the terminal cancel state is already written.

Internal helpers (not part of the public surface):
    - :func:`_wait_for_client` — briefly poll for the worker's connected client +
      loop, turning a cold-start race into a short wait instead of a 500.
    - :func:`_run_async` — run a coroutine on the worker loop and translate a
      ``Future`` timeout into a documented ``RuntimeError``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from agent_team_studio.user_agent_founder.temporal import (
    TASK_QUEUE,
    WORKFLOW_ID_PREFIX,
    UserAgentFounderWorkflow,
)
from shared.temporal import get_temporal_client, get_temporal_loop, signal_workflow_sync

logger = logging.getLogger(__name__)

START_WORKFLOW_TIMEOUT = 30
# How long to wait for the Temporal worker thread to connect its client
# before giving up. The worker connects in a daemon thread, so the very
# first request after a cold start can race the connect call.
CLIENT_READY_TIMEOUT_S = 10.0
CLIENT_READY_POLL_S = 0.05
# Cancellation is best-effort and user-facing: cap BOTH legs of the round trip so
# a /cancel never blocks on the defaults — the client-ready wait
# (CLIENT_READY_TIMEOUT_S, 10s) and separately the signal RPC itself
# (shared.temporal.runner.START_WORKFLOW_TIMEOUT_S, 30s) — when the worker is
# down or the Temporal server is slow to answer.
CANCEL_CLIENT_READY_TIMEOUT_S = 1.0
CANCEL_SIGNAL_RPC_TIMEOUT_S = 3.0


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
    """Run ``coro`` on the worker's asyncio loop from this sync caller's thread.

    Preconditions:
        - The Temporal client + loop are available (see :func:`_wait_for_client`).
    Postconditions:
        - Returns the coroutine's result, or re-raises whatever it raised.
        - Raises ``RuntimeError`` (not the bare ``TimeoutError`` that
          ``Future.result`` would otherwise surface) if ``coro`` has not
          completed within ``START_WORKFLOW_TIMEOUT`` seconds, so every caller
          sees one documented failure mode instead of an undocumented stdlib one.
    """
    _, loop = _wait_for_client()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        return future.result(timeout=START_WORKFLOW_TIMEOUT)
    except TimeoutError as exc:
        raise RuntimeError(
            f"Temporal call did not complete within {START_WORKFLOW_TIMEOUT}s"
        ) from exc


def start_founder_workflow(run_id: str) -> None:
    """Start ``UserAgentFounderWorkflow`` for the given run id.

    Preconditions:
        - ``run_id`` refers to a founder run + central job already created.
    Postconditions:
        - The workflow is started with id ``f"{WORKFLOW_ID_PREFIX}{run_id}"`` on
          ``TASK_QUEUE``. Raises ``RuntimeError`` if the worker client never
          becomes available or the start call does not complete within
          ``START_WORKFLOW_TIMEOUT`` seconds (see :func:`_run_async`) — callers
          (``_dispatch_founder_run``) rely on this being the sole exception type
          for "Temporal is enabled but the workflow failed to start".
    """
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


def cancel_founder_workflow(run_id: str) -> None:
    """Signal ``UserAgentFounderWorkflow`` to cancel cooperatively.

    Delivers the ``cancel`` signal so the workflow's poll loops short-circuit at
    the next tick instead of continuing to spend on target-team polls/answers.
    The API cancel route already wrote the terminal cancel state (central job
    CANCELLED; run row "failed" — the run store has no CANCELLED status); this only
    stops the in-flight workflow.

    Preconditions:
        - ``run_id`` refers to a run whose workflow may be running (a no-op if it
          has already ended — signalling a terminal workflow is accepted by the
          server / surfaces as a handled error the caller treats as best-effort).
    Postconditions:
        - The ``cancel`` signal is delivered to workflow id
          ``f"{WORKFLOW_ID_PREFIX}{run_id}"`` (raises ``RuntimeError``/times out
          if the worker client never becomes available within
          ``CANCEL_CLIENT_READY_TIMEOUT_S``, or if the signal RPC itself doesn't
          complete within ``CANCEL_SIGNAL_RPC_TIMEOUT_S``).
    """
    workflow_id = f"{WORKFLOW_ID_PREFIX}{run_id}"
    # Best-effort signal: cap BOTH the client-ready wait and the signal RPC's own
    # timeout so the /cancel handler stays snappy in both failure modes — the
    # worker client isn't up yet, or it's up but the Temporal server itself is
    # slow/unreachable (the store already recorded the terminal cancel; this only
    # stops an in-flight workflow). Without these caps this could block up to
    # CLIENT_READY_TIMEOUT_S (10s) plus the signal helper's own 30s RPC default.
    signal_workflow_sync(
        workflow_id,
        "cancel",
        client_ready_timeout_s=CANCEL_CLIENT_READY_TIMEOUT_S,
        timeout_s=CANCEL_SIGNAL_RPC_TIMEOUT_S,
    )
    logger.info("Signalled cancel to UserAgentFounderWorkflow id=%s", workflow_id)
