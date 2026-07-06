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
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_RUNNING,
    JobServiceClient,
)
from sales_team.models import SalesPipelineRequest
from sales_team.orchestrator import SalesPodOrchestrator

logger = logging.getLogger(__name__)

job_manager = JobServiceClient(team="sales_team")


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def run_pipeline_job(job_id: str, request: SalesPipelineRequest) -> None:
    """Run the sales pod orchestrator end-to-end and record job status.

    Preconditions:
        - ``job_id`` refers to a job already created in the job store.

    Postconditions:
        - On success the job store row ends in COMPLETED with the
          orchestrator result.
        - On failure the job store row ends in FAILED with the exception
          message. This function never raises — the thread-dispatch path
          and the Temporal activity both observe the outcome via the job
          store, not via a propagated exception.
    """
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
