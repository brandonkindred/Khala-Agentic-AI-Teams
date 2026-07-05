"""Temporal workflow + activity for the job matching team.

Kept in its own module (separate from the package ``__init__``) so the
temporalio workflow sandbox can re-import the workflow class without
executing any non-deterministic top-level code (e.g. ``os.getenv``,
worker bootstrap). Co-locating ``start_team_worker``/``is_temporal_enabled``
with the workflow class trips the sandbox with
``__call__ on os.getenv restricted`` during workflow registration.

The activity mirrors the API's thread-mode ``_run_scan_background`` so the
job-store state machine (PENDING -> RUNNING -> COMPLETED/FAILED) is
identical whether a scan runs on a daemon thread or a Temporal worker.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import activity, workflow
from temporalio.common import RetryPolicy


@activity.defn(name="job_matching_run_scan")
def run_scan_activity(job_id: str, request: dict[str, Any]) -> dict[str, Any]:
    """Run one scan on the Temporal worker, keeping the job store in sync.

    Reconstructs the request + orchestrator inside the activity because
    neither is serialisable across the Temporal boundary. Mirrors the API's
    ``_run_scan_background``: it drives the shared job store through
    RUNNING -> COMPLETED (or FAILED) and honours cooperative cancellation.

    Business exceptions are recorded on the job store as FAILED and
    **swallowed** (not re-raised), so a deterministic failure does not trigger
    Temporal's retry loop. A genuine worker/process crash leaves the activity
    task unfinished, which Temporal retries (bounded — see the workflow's
    ``RetryPolicy``) — that is what makes an in-flight scan survive a restart.
    A retry that lands on an already-COMPLETED job short-circuits and returns
    the stored result, so a crash that lost only the activity result never
    re-runs a finished scan; a crash mid-run (before COMPLETED) re-runs, bounded
    by the retry policy.

    Preconditions:
        * ``job_id`` refers to a job row already created by ``POST /scan``.
        * ``request`` is the JSON dump of a :class:`JobMatchRequest`.
    Postconditions:
        * The job row is COMPLETED (with the serialised response) on success,
          FAILED (with the error) on failure, and left untouched if the job was
          cancelled before or during the run.
        * Idempotent on retry: when the job is already COMPLETED, returns the
          stored result without re-running or mutating the job row.
        * The return value is the serialised :class:`JobMatchResponse` on
          success, else an empty dict.
    """
    from job_matching_team.models import JobMatchRequest
    from job_matching_team.orchestrator import JobMatchingOrchestrator
    from job_matching_team.shared.job_store import (
        JOB_STATUS_CANCELLED,
        JOB_STATUS_COMPLETED,
        JOB_STATUS_FAILED,
        JOB_STATUS_RUNNING,
        get_job,
        is_job_cancelled,
        update_job,
    )

    req = JobMatchRequest(**request)
    try:
        # Idempotent replay: if a prior attempt already COMPLETED this scan (a
        # crash after update_job(COMPLETED) but before Temporal recorded the
        # result triggers a retry), return the stored result instead of
        # re-running — no status flap, no duplicate scan/LLM spend.
        existing = get_job(job_id)
        if existing is not None:
            status = existing.get("status")
            if status == JOB_STATUS_COMPLETED:
                return existing.get("result") or {}
            # Derive the pre-run cancellation check from the row we just read
            # instead of a second job-service round-trip. The post-run check
            # below still needs a fresh read (cancellation can happen mid-scan).
            if status == JOB_STATUS_CANCELLED:
                return {}
        update_job(job_id, status=JOB_STATUS_RUNNING)
        result = JobMatchingOrchestrator().run(req, job_id=job_id)
        if is_job_cancelled(job_id):
            return {}
        payload = result.model_dump(mode="json")
        update_job(job_id, status=JOB_STATUS_COMPLETED, result=payload)
        return payload
    except Exception as exc:  # noqa: BLE001 - recorded on the job store, not re-raised
        activity.logger.exception("Job matching scan %s failed", job_id)
        if not is_job_cancelled(job_id):
            update_job(job_id, status=JOB_STATUS_FAILED, error=str(exc))
        return {}


@workflow.defn(name="JobMatchingWorkflow")
class JobMatchingWorkflow:
    @workflow.run
    async def run(self, job_id: str, request: dict[str, Any]) -> dict[str, Any]:
        return await workflow.execute_activity(
            run_scan_activity,
            args=[job_id, request],
            start_to_close_timeout=timedelta(minutes=30),
            # The activity swallows business failures (records FAILED, returns),
            # so retries only fire on a worker/process crash. Bound them: the scan
            # is not idempotent, so unlimited retries would re-run full scans.
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
