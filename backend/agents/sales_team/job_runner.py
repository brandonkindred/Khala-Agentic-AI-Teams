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
TERMINAL_STATUSES = frozenset(
    {JOB_STATUS_COMPLETED, JOB_STATUS_FAILED, JOB_STATUS_CANCELLED, JOB_STATUS_INTERRUPTED}
)
# Terminal states that are NOT failures: a cancel/interrupt (and an
# already-completed replay) end a run cleanly, whereas FAILED must surface as
# a failed Temporal workflow rather than be masked as success.
CLEAN_TERMINAL_STATUSES = frozenset(
    {JOB_STATUS_COMPLETED, JOB_STATUS_CANCELLED, JOB_STATUS_INTERRUPTED}
)
_TERMINAL_STATUSES = TERMINAL_STATUSES


def now_iso() -> str:
    """Current UTC time as an ISO-8601 string (the job store's timestamp shape).

    Postconditions: returns a timezone-aware ISO-8601 string in UTC.
    """
    return datetime.now(tz=timezone.utc).isoformat()


_now = now_iso


def write_job_progress(job_id: str, stage: str, pct: int) -> None:
    """Record stage progress on the job row.

    Shared by the thread path's ``on_update`` callback and the Temporal
    ``sales_report_progress`` activity so both modes persist the identical
    field set.

    Preconditions:
        - ``job_id`` refers to an existing job row.
    Postconditions:
        - ``current_stage``/``progress``/``last_updated_at`` are updated;
          raises if the job-store write fails (callers decide retry policy).
    """
    job_manager.update_job(job_id, current_stage=stage, progress=pct, last_updated_at=now_iso())


def write_job_failed(job_id: str, error: str) -> None:
    """Record the terminal FAILED state on the job row.

    Shared by the thread path's exception handler and the Temporal
    ``sales_mark_failed`` activity so both modes persist the identical
    failure row.

    Preconditions:
        - ``job_id`` refers to an existing job row.
    Postconditions:
        - The row ends in FAILED with ``error`` recorded; raises if the write
          itself fails (callers decide whether that is fatal).
    """
    job_manager.update_job(
        job_id,
        status=JOB_STATUS_FAILED,
        current_stage="failed",
        error=error,
        eta_hint=None,
        last_updated_at=now_iso(),
    )


def write_job_completed(job_id: str, result: dict) -> None:
    """Record the terminal COMPLETED state with the pipeline result.

    Shared by the thread path and the Temporal ``sales_finalize`` activity.

    Preconditions:
        - ``job_id`` refers to an existing job row.
        - ``result`` is the JSON-shaped pipeline result payload.
    Postconditions:
        - The row ends in COMPLETED at 100% with ``result`` attached; raises
          if the write fails.
    """
    job_manager.update_job(
        job_id,
        status=JOB_STATUS_COMPLETED,
        current_stage="completed",
        progress=100,
        eta_hint="done",
        result=result,
        last_updated_at=now_iso(),
    )


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
    if existing.get("status") in TERMINAL_STATUSES:
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
            write_job_progress(job_id, stage, pct)

        result = orchestrator.run(request, job_id=job_id, update_cb=on_update)

        # A cancel (or stale-failure) can land while the orchestrator runs;
        # don't clobber that terminal status with COMPLETED.
        current = job_manager.get_job(job_id)
        if current is not None and current.get("status") in TERMINAL_STATUSES:
            logger.info(
                "Sales pipeline job %s went terminal (%s) during run; not writing COMPLETED",
                job_id,
                current.get("status"),
            )
            return

        write_job_completed(job_id, result.model_dump())
    except Exception as exc:
        logger.error("Sales pipeline job %s failed: %s", job_id, exc, exc_info=True)
        write_job_failed(job_id, str(exc))
