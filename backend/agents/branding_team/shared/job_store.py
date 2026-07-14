"""Job store for the Branding team — backed by JobServiceClient.

The standard create/get/update/list + cancel/is-cancelled/delete + shutdown-sweep
wrappers come from the shared ``job_store_factory`` so this module only owns the
team's client singleton.
"""

from __future__ import annotations

from typing import Optional

from job_service_client import (
    JOB_STATUS_CANCELLED,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    JobServiceClient,
)
from job_store_factory import make_status_job_store

__all__ = [
    "JOB_STATUS_CANCELLED",
    "JOB_STATUS_COMPLETED",
    "JOB_STATUS_FAILED",
    "JOB_STATUS_PENDING",
    "JOB_STATUS_RUNNING",
    "begin_job",
    "cancel_job",
    "create_job",
    "delete_job",
    "get_job",
    "is_job_cancelled",
    "list_jobs",
    "mark_all_running_jobs_failed",
    "mark_completed",
    "mark_failed",
    "update_job",
]

_client_instance: Optional[JobServiceClient] = None


def _client() -> JobServiceClient:
    global _client_instance
    if _client_instance is None:
        _client_instance = JobServiceClient(team="branding_team")
    return _client_instance


# Standard status wrappers, bound to this team's client. The lambda resolves
# ``_client`` by name on every call so tests can monkeypatch ``job_store._client``.
_store = make_status_job_store(lambda: _client())
create_job = _store.create_job
get_job = _store.get_job
update_job = _store.update_job
list_jobs = _store.list_jobs
cancel_job = _store.cancel_job
is_job_cancelled = _store.is_job_cancelled
delete_job = _store.delete_job
mark_all_running_jobs_failed = _store.mark_all_running_jobs_failed


def begin_job(job_id: str) -> bool:
    """Mark ``job_id`` RUNNING unless it was already cancelled.

    The single guarded entry point for the RUNNING transition, shared by the
    thread path (``api.main._run_branding_core``) and the Temporal activity
    (``temporal.activities.begin_branding_job_activity``) so the cancel-check
    lives in exactly one place.

    Preconditions:
        - ``job_id`` refers to a job row already created in the job store.
    Postconditions:
        - Returns False and makes no write if the job is already cancelled.
        - Otherwise writes status=RUNNING and returns True.
    """
    if is_job_cancelled(job_id):
        return False
    update_job(job_id, status=JOB_STATUS_RUNNING)
    return True


def mark_completed(job_id: str, result: dict) -> bool:
    """Mark ``job_id`` COMPLETED with ``result`` unless it was already cancelled.

    Preconditions:
        - ``job_id`` refers to an existing job row; ``result`` is a JSON-serializable
          mapping (e.g. ``TeamOutput.model_dump()``).
    Postconditions:
        - Returns False and makes no write if the job is already cancelled (a
          cancelled run is terminal, not a completion).
        - Otherwise writes status=COMPLETED with ``result`` and returns True.
    """
    if is_job_cancelled(job_id):
        return False
    update_job(job_id, status=JOB_STATUS_COMPLETED, result=result)
    return True


def mark_failed(job_id: str, error: str) -> bool:
    """Mark ``job_id`` FAILED with ``error`` unless it was already cancelled.

    Preconditions:
        - ``job_id`` refers to an existing job row; ``error`` is a short message.
    Postconditions:
        - Returns False and makes no write if the job is already cancelled (a
          cancelled run is terminal, not a failure).
        - Otherwise writes status=FAILED with ``error`` and returns True.
    """
    if is_job_cancelled(job_id):
        return False
    update_job(job_id, status=JOB_STATUS_FAILED, error=error)
    return True
