"""Start-and-await the code review workflow from synchronous code.

``CodeReviewAgent.run`` is a synchronous call that must return a
``CodeReviewOutput``, so — unlike the fire-and-forget
``shared.temporal.start_workflow_sync`` — this bridge *executes* the workflow and
blocks for its result on the worker's asyncio loop.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any, Dict

from shared.temporal.runner import _await_client

from .config import TASK_QUEUE, resolve_execute_timeout_s, resolve_execution_timeout_s

logger = logging.getLogger(__name__)

# Resolved once at import via CODE_REVIEW_EXECUTE_TIMEOUT_S (see
# resolve_execute_timeout_s's docstring for the defensive-parsing contract and
# docs/ENV_VARS.md for the operator-facing default/floor/caveat). A caller
# that needs the live value on every call (e.g. agent.py's _run_via_temporal,
# which also uses it to build a client-timeout error message) calls
# resolve_execute_timeout_s() directly rather than reading this
# frozen-at-first-import constant.
EXECUTE_TIMEOUT_S = resolve_execute_timeout_s()


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
        - The Temporal-side ``execution_timeout`` (derived from
          ``execute_timeout_s`` via ``resolve_execution_timeout_s`` — strictly
          less, by ``EXECUTION_TIMEOUT_MARGIN_S``) reclaims an abandoned
          execution's worker slots at essentially the same moment this call's
          own wait gives up, instead of leaving it to run unbounded
          server-side.

    Raises:
        - ``RuntimeError`` if the worker's Temporal client never becomes available
          within the wait window (the caller treats this as "Temporal
          unavailable" and falls back to in-process review).
        - ``TimeoutError`` (bare, no message) if THIS call's own wait exceeds
          ``execute_timeout_s`` — the workflow may still be running, or may
          have already completed, server-side. Callers that want a
          diagnosable message should catch this and attach context (see
          ``agent.py``'s ``_run_via_temporal``).
        - ``temporalio.client.WorkflowFailureError`` if the workflow itself
          fails (the caller unwraps a ``CodeReviewUnavailableError`` marker
          from it) — this now also covers the Temporal-side
          ``execution_timeout`` expiring (wrapping a
          ``temporalio.exceptions.TimeoutError``, a message-bearing exception,
          distinct from the bare client-side ``TimeoutError`` above).
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
        execution_timeout=timedelta(seconds=resolve_execution_timeout_s(execute_timeout_s)),
    )
    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=execute_timeout_s)
