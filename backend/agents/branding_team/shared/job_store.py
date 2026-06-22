"""Job store for the Branding team — a thin alias over the shared BaseJobStore.

The CRUD logic lives in ``shared_job_store.BaseJobStore``; this module just
binds it to the ``branding_team`` namespace and re-exports the module-level
function API that callers already import.
"""

from __future__ import annotations

import os
from pathlib import Path

from shared_job_store import (
    JOB_STATUS_CANCELLED,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    BaseJobStore,
)

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
]

DEFAULT_CACHE_DIR: Path = Path(os.environ.get("AGENT_CACHE", ".agent_cache"))

_store = BaseJobStore(team="branding_team")

create_job = _store.create_job
get_job = _store.get_job
update_job = _store.update_job
list_jobs = _store.list_jobs
cancel_job = _store.cancel_job
is_job_cancelled = _store.is_job_cancelled
delete_job = _store.delete_job
mark_all_running_jobs_failed = _store.mark_all_running_jobs_failed
