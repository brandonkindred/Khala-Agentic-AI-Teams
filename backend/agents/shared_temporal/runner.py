"""Shared job-backed workflow runner.

``run_team_job`` is the one entrypoint teams use from their sync HTTP
handlers. It:

1. Ensures a row exists in the team's job store (via ``JobServiceClient``).
2. If Temporal is enabled, starts the given workflow on the shared client
   with a deterministic ID ``{team}-{job_id}`` so re-submits are idempotent
   and mid-flight failures resume from the last completed activity.
3. If Temporal is disabled (local dev), falls back to running the provided
   ``fallback`` callable in a background thread so behavior is identical to
   the current thread-mode code path.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Callable, Optional

from shared_temporal.client import (
    get_default_task_queue,
    get_temporal_client,
    get_temporal_loop,
    is_temporal_enabled,
)

logger = logging.getLogger(__name__)


def _get_job_manager(team: str) -> Any:
    from job_service_client import JobServiceClient

    return JobServiceClient(team=team)


def run_team_job(
    team: str,
    job_id: str,
    workflow: Any,
    workflow_args: Optional[list[Any]] = None,
    *,
    task_queue: Optional[str] = None,
    fallback: Optional[Callable[[], Any]] = None,
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
        fallback: Callable invoked in a daemon thread when Temporal is
            disabled. Should update the job record itself.
        metadata: Extra fields to persist on the initial job record.
    """
    manager = _get_job_manager(team)
    init_fields = {"status": "pending"}
    if metadata:
        init_fields.update(metadata)
    manager.create_job(job_id, **init_fields)

    if is_temporal_enabled():
        client = get_temporal_client()
        loop = get_temporal_loop()
        if client is None or loop is None:
            raise RuntimeError(
                "Temporal is enabled but client is not connected; ensure the team's "
                "worker started during app lifespan."
            )
        queue = task_queue or get_default_task_queue()
        workflow_id = f"{team}-{job_id}"
        coro = client.start_workflow(
            workflow,
            *(workflow_args or []),
            id=workflow_id,
            task_queue=queue,
        )
        asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=30)
        manager.update_job(job_id, status="running", workflow_id=workflow_id)
        return {"job_id": job_id, "team": team, "status": "running", "mode": "temporal"}

    if fallback is None:
        manager.update_job(job_id, status="failed", error="No fallback configured")
        raise RuntimeError(
            f"Team {team} has no thread-mode fallback and Temporal is not enabled."
        )

    def _run_fallback() -> None:
        try:
            manager.update_job(job_id, status="running")
            fallback()
        except Exception as e:
            logger.exception("Thread-mode job failed: team=%s job=%s", team, job_id)
            manager.update_job(job_id, status="failed", error=str(e))

    threading.Thread(
        target=_run_fallback,
        name=f"{team}-job-{job_id}",
        daemon=True,
    ).start()
    return {"job_id": job_id, "team": team, "status": "running", "mode": "thread"}
