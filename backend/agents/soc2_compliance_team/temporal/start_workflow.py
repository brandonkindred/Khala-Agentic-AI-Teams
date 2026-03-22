"""Start SOC2 Temporal workflows from sync API."""

from __future__ import annotations

import asyncio
import logging

from soc2_compliance_team.temporal.client import (
    get_temporal_client,
    get_temporal_loop,
)
from soc2_compliance_team.temporal.constants import TASK_QUEUE, WORKFLOW_ID_PREFIX_AUDIT
from soc2_compliance_team.temporal.workflows import Soc2AuditWorkflow

logger = logging.getLogger(__name__)

START_WORKFLOW_TIMEOUT = 30


def _run_async(coro: object) -> object:
    loop = get_temporal_loop()
    client = get_temporal_client()
    if loop is None or client is None:
        raise RuntimeError("Temporal client not available; is the SOC2 worker running?")
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=START_WORKFLOW_TIMEOUT)


def start_audit_workflow(job_id: str, repo_path: str) -> None:
    """Start Soc2AuditWorkflow for the given job."""
    client = get_temporal_client()
    if client is None:
        raise RuntimeError("Temporal client not available")
    workflow_id = f"{WORKFLOW_ID_PREFIX_AUDIT}{job_id}"
    _run_async(
        client.start_workflow(
            Soc2AuditWorkflow.run,
            args=[job_id, repo_path],
            id=workflow_id,
            task_queue=TASK_QUEUE,
        )
    )
    logger.info("Started Soc2AuditWorkflow id=%s", workflow_id)
