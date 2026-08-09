"""Job store for the Investment team's async backtest jobs.

The standard create/get/update/list + cancel/is-cancelled/delete wrappers come
from the shared ``job_store_factory`` so this module only owns the team's client
singleton.
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
    "cancel_job",
    "create_job",
    "delete_job",
    "get_job",
    "is_job_cancelled",
    "list_jobs",
    "mark_all_running_jobs_failed",
    "update_job",
    "update_job_if_not_cancelled",
]

_client_instance: Optional[JobServiceClient] = None


def _client() -> JobServiceClient:
    global _client_instance
    if _client_instance is None:
        _client_instance = JobServiceClient(team="investment_backtests")
    return _client_instance


# Standard status wrappers, bound to this team's client. The lambda resolves
# ``_client`` by name on every call so tests can monkeypatch ``job_store._client``.
_store = make_status_job_store(lambda: _client())
create_job = _store.create_job
get_job = _store.get_job
update_job = _store.update_job
update_job_if_not_cancelled = _store.update_job_if_not_cancelled
list_jobs = _store.list_jobs
cancel_job = _store.cancel_job
is_job_cancelled = _store.is_job_cancelled
delete_job = _store.delete_job
mark_all_running_jobs_failed = _store.mark_all_running_jobs_failed
