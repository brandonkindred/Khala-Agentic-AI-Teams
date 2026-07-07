"""Temporal workflow + activity wrapping the deepthought orchestrator.

Follows shared_temporal Pattern A: exports ``WORKFLOWS``/``ACTIVITIES`` that
wrap the same ``DeepthoughtOrchestrator().process_message(...)`` call the API
uses. The worker is started by ``deepthought.temporal.worker`` (invoked by the
team_service entrypoint via ``TEAM_TEMPORAL_WORKER_MODULE`` /
``TEAM_TEMPORAL_WORKER_FUNC``), so importing this module has no side effects.

The activity owns the same job-store status transitions as the thread path
(``deepthought.api.main._run_deepthought_background``), so ``/status/{job_id}``
polling works identically whether a job runs on a thread or a Temporal worker.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from datetime import timedelta
from typing import Any

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

logger = logging.getLogger(__name__)

TASK_QUEUE = "deepthought-queue"
WORKFLOW_ID_PREFIX = "deepthought-"

# One activity attempt: an application-level orchestrator failure is already
# recorded as FAILED in the job store, so retrying would silently re-run the
# expensive multi-agent pipeline (and re-charge LLM cost). Worker/process loss
# still reschedules an *incomplete* activity — that is the durability benefit.
_RUN_RETRY_POLICY = RetryPolicy(maximum_attempts=1)


@activity.defn(name="deepthought_run_pipeline")
def run_pipeline_activity(job_id: str, request: dict[str, Any]) -> dict[str, Any]:
    """Run the deepthought orchestrator and record the result in the job store.

    Mirrors ``deepthought.api.main._run_deepthought_background``: flips the job
    to RUNNING, executes the orchestrator, then writes COMPLETED (with the
    result) or FAILED. Imports are deferred so the worker never pulls in the
    FastAPI app module.

    Preconditions:
        - ``job_id`` refers to a job row already created by the caller
          (``create_job`` runs in the API handler before dispatch).
        - ``request`` is a ``DeepthoughtRequest.model_dump()`` payload.

    Postconditions:
        - On success: job status is COMPLETED with ``result`` set, and the
          orchestrator result dict is returned.
        - On failure: job status is FAILED with ``error`` set, and the
          exception is re-raised so Temporal records a failed workflow.
        - If the job was cancelled before this activity ran, returns ``{}``
          without touching the orchestrator or job status.
    """
    from deepthought.models import DeepthoughtRequest
    from deepthought.orchestrator import DeepthoughtOrchestrator
    from deepthought.shared.job_store import (
        JOB_STATUS_COMPLETED,
        JOB_STATUS_FAILED,
        JOB_STATUS_RUNNING,
        is_job_cancelled,
        update_job,
    )

    if is_job_cancelled(job_id):
        return {}

    try:
        update_job(job_id, status=JOB_STATUS_RUNNING)
        req = DeepthoughtRequest(**request)
        result = DeepthoughtOrchestrator().process_message(req)
        if inspect.iscoroutine(result):
            result = asyncio.new_event_loop().run_until_complete(result)
        dump = result.model_dump() if hasattr(result, "model_dump") else result
        dump = dump if isinstance(dump, dict) else {"result": dump}
        if is_job_cancelled(job_id):
            return dump
        update_job(job_id, status=JOB_STATUS_COMPLETED, result=dump)
        return dump
    except Exception as e:  # noqa: BLE001 — record then re-raise for Temporal
        logger.exception("Deepthought job %s failed", job_id)
        if not is_job_cancelled(job_id):
            update_job(job_id, status=JOB_STATUS_FAILED, error=str(e))
        raise


@workflow.defn(name="DeepthoughtWorkflow")
class DeepthoughtWorkflow:
    """Durable wrapper around a single deepthought orchestrator run.

    Invariants:
        - Exactly one ``run_pipeline_activity`` execution per workflow run;
          the activity owns all job-store status writes.
    """

    @workflow.run
    async def run(self, job_id: str, request: dict[str, Any]) -> dict[str, Any]:
        """Execute the deepthought pipeline for ``job_id``.

        Preconditions:
            - ``job_id`` is non-empty and refers to an existing job row.
            - ``request`` is a ``DeepthoughtRequest.model_dump()`` payload.

        Postconditions:
            - Returns the orchestrator result dict, or raises if the activity
              failed (already recorded as FAILED in the job store).
        """
        return await workflow.execute_activity(
            run_pipeline_activity,
            args=[job_id, request],
            start_to_close_timeout=timedelta(hours=1),
            retry_policy=_RUN_RETRY_POLICY,
        )


WORKFLOWS = [DeepthoughtWorkflow]
ACTIVITIES = [run_pipeline_activity]

__all__ = [
    "ACTIVITIES",
    "DeepthoughtWorkflow",
    "TASK_QUEUE",
    "WORKFLOWS",
    "WORKFLOW_ID_PREFIX",
    "run_pipeline_activity",
]
