"""Sales pipeline job execution — shared by the thread-dispatch path and the
Temporal activity.

Kept free of any import of ``sales_team.api.main`` (and therefore of FastAPI
route registration, the stale-job monitor, and the invoke shim) so the
Temporal activity can reuse this logic without dragging in application
bootstrap side effects if it is ever invoked from a process that hasn't
already imported the API module.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from job_service_client import (
    JOB_STATUS_CANCELLED,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_INTERRUPTED,
    JOB_STATUS_RUNNING,
    JobServiceClient,
)
from sales_team.models import SalesPipelineRequest
from sales_team.orchestrator import SalesPodOrchestrator

logger = logging.getLogger(__name__)

job_manager = JobServiceClient(team="sales_team")

# A job that has already reached one of these states must not be (re)started or
# have its status overwritten. Under Temporal, a workflow can sit queued (worker
# saturated) long enough for the cancel endpoint or the 300s stale-job monitor
# to move the row terminal before the activity ever runs; running anyway would
# resurrect it to RUNNING/COMPLETED and burn LLM work after cancellation.
_TERMINAL_STATUSES = frozenset(
    {JOB_STATUS_COMPLETED, JOB_STATUS_FAILED, JOB_STATUS_CANCELLED, JOB_STATUS_INTERRUPTED}
)


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def run_pipeline_job(job_id: str, request: SalesPipelineRequest) -> None:
    """Run the sales pod orchestrator end-to-end and record job status.

    Preconditions:
        - ``job_id`` refers to a job already created in the job store.

    Postconditions:
        - If the job is missing or already in a terminal state
          (completed/failed/cancelled/interrupted) when this runs, the
          orchestrator is NOT started and the row is left untouched — a
          queued Temporal workflow cannot resurrect a cancelled/stale job.
        - On success the job store row ends in COMPLETED with the
          orchestrator result — unless the job was moved terminal (e.g.
          cancelled) while the orchestrator ran, in which case that terminal
          status is preserved rather than overwritten with COMPLETED.
        - On failure the job store row ends in FAILED with the exception
          message. This function never raises — the thread-dispatch path
          and the Temporal activity both observe the outcome via the job
          store, not via a propagated exception.
    """
    existing = job_manager.get_job(job_id)
    if existing is None:
        logger.warning("Sales pipeline job %s not found at start; skipping run", job_id)
        return
    if existing.get("status") in _TERMINAL_STATUSES:
        logger.info(
            "Sales pipeline job %s already terminal (%s) before start; skipping run",
            job_id,
            existing.get("status"),
        )
        return

    try:
        job_manager.update_job(
            job_id,
            status=JOB_STATUS_RUNNING,
            current_stage="initializing",
            progress=2,
            eta_hint="Starting pipeline...",
        )

        orchestrator = SalesPodOrchestrator(config=request.config)

        def on_update(stage: str, pct: int) -> None:
            job_manager.update_job(
                job_id, current_stage=stage, progress=pct, last_updated_at=_now()
            )

        result = orchestrator.run(request, job_id=job_id, update_cb=on_update)

        # A cancel (or stale-failure) can land while the orchestrator runs;
        # don't clobber that terminal status with COMPLETED.
        current = job_manager.get_job(job_id)
        if current is not None and current.get("status") in _TERMINAL_STATUSES:
            logger.info(
                "Sales pipeline job %s went terminal (%s) during run; not writing COMPLETED",
                job_id,
                current.get("status"),
            )
            return

        job_manager.update_job(
            job_id,
            status=JOB_STATUS_COMPLETED,
            current_stage="completed",
            progress=100,
            eta_hint="done",
            result=result.model_dump(),
            last_updated_at=_now(),
        )
    except Exception as exc:
        logger.error("Sales pipeline job %s failed: %s", job_id, exc, exc_info=True)
        job_manager.update_job(
            job_id,
            status=JOB_STATUS_FAILED,
            current_stage="failed",
            error=str(exc),
            eta_hint=None,
            last_updated_at=_now(),
        )
