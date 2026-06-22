"""Shared base job store — the common CRUD over ``JobServiceClient``.

Most teams wrap ``JobServiceClient`` in a ``shared/job_store.py`` module that is
byte-for-byte the same apart from the team name: a lazily-created client
singleton plus ``create/get/update/list/cancel/delete`` helpers. This module
factors that out so a team's job store becomes a few lines:

    from shared_job_store import BaseJobStore, JOB_STATUS_PENDING, ...

    _store = BaseJobStore(team="my_team")
    create_job = _store.create_job
    get_job = _store.get_job
    ...

The module-level function API each team already exposes is preserved by
re-binding the methods, so callers (``from my_team.shared.job_store import
create_job``) are unaffected.

Invariants:
    - Exactly one ``JobServiceClient`` is created per ``BaseJobStore`` instance,
      lazily on first use.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from job_service_client import (
    JOB_STATUS_CANCELLED,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    JobServiceClient,
)

__all__ = [
    "JOB_STATUS_CANCELLED",
    "JOB_STATUS_COMPLETED",
    "JOB_STATUS_FAILED",
    "JOB_STATUS_PENDING",
    "JOB_STATUS_RUNNING",
    "BaseJobStore",
]

logger = logging.getLogger(__name__)

# Statuses from which a job may still be cancelled.
_CANCELLABLE = {JOB_STATUS_PENDING, JOB_STATUS_RUNNING}


class BaseJobStore:
    """Thin, reusable wrapper over ``JobServiceClient`` for one team.

    Preconditions: ``team`` is a non-empty job-service team namespace.
    Postconditions: instance methods proxy to a single lazily-created client.
    """

    def __init__(self, team: str) -> None:
        assert team, "team must be a non-empty string"
        self._team = team
        self._client_instance: Optional[JobServiceClient] = None

    def _client(self) -> JobServiceClient:
        if self._client_instance is None:
            self._client_instance = JobServiceClient(team=self._team)
        return self._client_instance

    def create_job(self, job_id: str, **fields: Any) -> None:
        """Create a job in the PENDING state (callers may override via ``status=``)."""
        fields.setdefault("status", JOB_STATUS_PENDING)
        self._client().create_job(job_id, **fields)

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        return self._client().get_job(job_id)

    def update_job(self, job_id: str, **fields: Any) -> None:
        self._client().update_job(job_id, **fields)

    def list_jobs(self, statuses: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        return self._client().list_jobs(statuses=statuses)

    def cancel_job(self, job_id: str) -> bool:
        """Cancel a job if it is still pending/running.

        Postconditions: returns True iff the job existed in a cancellable state
            and was transitioned to CANCELLED.
        """
        job = self._client().get_job(job_id)
        if job is None or job.get("status") not in _CANCELLABLE:
            return False
        self._client().update_job(job_id, status=JOB_STATUS_CANCELLED)
        return True

    def is_job_cancelled(self, job_id: str) -> bool:
        """Return True if the job exists and has been marked cancelled."""
        job = self._client().get_job(job_id)
        return job is not None and job.get("status") == JOB_STATUS_CANCELLED

    def delete_job(self, job_id: str) -> bool:
        return bool(self._client().delete_job(job_id))

    def mark_all_running_jobs_failed(self, reason: str) -> None:
        """Best-effort: fail every active job (used on startup after a crash)."""
        try:
            self._client().mark_all_active_jobs_failed(reason)
        except Exception as e:  # pragma: no cover - defensive, network/IO dependent
            logger.warning("mark_all_running_jobs_failed: %s", e)
