"""Shared factory for the standard status-only job store used by simple async teams.

Several teams (market research, branding, deepthought, investment, startup advisor,
job matching) need the identical thin wrapper over ``JobServiceClient``: create /
get / update / list plus cancel / is-cancelled / delete and an optional shutdown
sweep. Each team used to copy-paste that block, which then drifted subtly. This
factory builds the bundle once, bound to a caller-supplied client getter, so those
modules stop duplicating it while keeping their own ``_client`` singleton (which
their test suites monkeypatch).

Invariants:
- Every operation delegates to the client returned by ``client_getter()``,
  resolved at call time — never a cached client object — so a test that rebinds
  the team module's ``_client`` is observed by all bound functions.
- ``cancel_job`` only transitions jobs currently ``pending``/``running``; jobs in
  any terminal (or missing) state are left untouched and report ``False``. This
  is an atomic conditional update server-side (via ``cancel_active_job``) — no
  read-then-write race.
- ``update_job_if_not_cancelled`` atomically writes fields (typically a status
  transition) unless the job is already cancelled, closing the same class of
  check-then-write race for RUNNING/COMPLETED/FAILED transitions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from job_service_client import (
    JOB_STATUS_CANCELLED,
    JOB_STATUS_PENDING,
    JobServiceClient,
)

logger = logging.getLogger(__name__)

ClientGetter = Callable[[], JobServiceClient]


@dataclass(frozen=True)
class StatusJobStore:
    """Bundle of standard job-store operations bound to one team's client.

    Invariant: each callable delegates to the ``client_getter`` resolved at call
    time (never a cached client object), preserving monkeypatchability.
    """

    create_job: Callable[..., None]
    get_job: Callable[[str], Optional[Dict[str, Any]]]
    update_job: Callable[..., None]
    update_job_if_not_cancelled: Callable[..., Optional[bool]]
    list_jobs: Callable[..., List[Dict[str, Any]]]
    cancel_job: Callable[[str], bool]
    is_job_cancelled: Callable[[str], bool]
    delete_job: Callable[[str], bool]
    mark_all_running_jobs_failed: Callable[[str], None]


def make_status_job_store(client_getter: ClientGetter) -> StatusJobStore:
    """Build the standard status job-store bundle for one team.

    Preconditions:
        ``client_getter`` is a zero-arg callable returning the team's
        ``JobServiceClient``. It is invoked (not cached) on every operation, so
        that rebinding it — as the team test suites do — takes effect.
    Postconditions:
        Returns a :class:`StatusJobStore` whose callables mirror the historical
        per-team wrappers exactly: ``create_job`` opens a job in ``pending``;
        ``cancel_job`` transitions only active jobs and reports whether it did;
        ``mark_all_running_jobs_failed`` is best-effort and never raises.
    """
    assert callable(client_getter), "client_getter must be a zero-arg callable"

    def create_job(job_id: str, **fields: Any) -> None:
        """Create ``job_id`` in ``pending`` status, merging ``fields``."""
        client_getter().create_job(job_id, status=JOB_STATUS_PENDING, **fields)

    def get_job(job_id: str) -> Optional[Dict[str, Any]]:
        """Return the job record, or ``None`` if it does not exist."""
        return client_getter().get_job(job_id)

    def update_job(job_id: str, **fields: Any) -> None:
        """Merge ``fields`` into the job record for ``job_id``."""
        client_getter().update_job(job_id, **fields)

    def update_job_if_not_cancelled(job_id: str, **fields: Any) -> Optional[bool]:
        """Merge ``fields`` into ``job_id`` unless it is already cancelled.

        Preconditions:
            ``fields["status"]``, if present, must not be ``JOB_STATUS_CANCELLED``
            — this is not a cancellation mechanism (use ``cancel_job``).
        Postconditions:
            Returns True iff the write happened (the job existed and was not
            cancelled). Returns False if the job exists but is cancelled. Returns
            None if the job does not exist at all — distinct from False so a
            caller can tell a broken precondition apart from a legitimate
            cancellation. Atomic — the cancelled-check and the write happen in
            one server-side conditional update, so a cancel landing between a
            caller's decision and this call can never be silently overwritten.
        """
        return client_getter().update_job_if_not_cancelled(job_id, **fields)

    def list_jobs(statuses: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """Return job records, optionally filtered to ``statuses``."""
        return client_getter().list_jobs(statuses=statuses)

    def cancel_job(job_id: str) -> bool:
        """Cancel ``job_id`` if it is currently pending/running.

        Postconditions: returns ``True`` and sets status ``cancelled`` when the
        job existed and was active; returns ``False`` with no write otherwise.
        Atomic — delegates to the client's conditional-update cancellation
        primitive, so there is no read-then-write race.
        """
        return client_getter().cancel_active_job(job_id)

    def is_job_cancelled(job_id: str) -> bool:
        """Return ``True`` if the job exists and is marked cancelled."""
        job = client_getter().get_job(job_id)
        return job is not None and job.get("status") == JOB_STATUS_CANCELLED

    def delete_job(job_id: str) -> bool:
        """Delete the job; return ``True`` if a record was removed."""
        return bool(client_getter().delete_job(job_id))

    def mark_all_running_jobs_failed(reason: str) -> None:
        """Best-effort: mark all active jobs failed (e.g. on server shutdown).

        Postconditions: never raises; a client error is logged and swallowed.
        """
        try:
            client_getter().mark_all_active_jobs_failed(reason)
        except Exception as e:
            logger.warning("mark_all_running_jobs_failed: %s", e)

    return StatusJobStore(
        create_job=create_job,
        get_job=get_job,
        update_job=update_job,
        update_job_if_not_cancelled=update_job_if_not_cancelled,
        list_jobs=list_jobs,
        cancel_job=cancel_job,
        is_job_cancelled=is_job_cancelled,
        delete_job=delete_job,
        mark_all_running_jobs_failed=mark_all_running_jobs_failed,
    )
