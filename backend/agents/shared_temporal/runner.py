"""Shared job-backed workflow runner.

``run_team_job`` is the one entrypoint teams use from their sync HTTP
handlers. It:

1. Ensures a row exists in the team's job store (via ``JobServiceClient``).
2. Starts the given workflow on the Temporal client with a deterministic
   ID ``{team}-{job_id}`` so re-submits are idempotent and mid-flight
   failures resume from the last completed activity.

Temporal is required. The system will fail fast if TEMPORAL_ADDRESS is not set.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

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
