"""Job store for the Branding team — backed by JobServiceClient.

The standard create/get/update/list + cancel/is-cancelled/delete + shutdown-sweep
wrappers come from the shared ``job_store_factory`` so this module only owns the
team's client singleton. It also exposes three guarded-transition helpers
(``begin_job``, ``mark_completed``, ``mark_failed``) that atomically check for
cancellation before writing a new status — the single place the thread path
(``api.main._run_branding_core``) and the Temporal activities
(``temporal.activities``) both go through for RUNNING/COMPLETED/FAILED writes.

The cancel-check and the status-write happen in one server-side conditional
update (``update_job_if_not_cancelled``), so a cancel landing between a caller's
decision and this write can never be silently overwritten — the same guarantee
``JobServiceClient.cancel_active_job`` provides for cancellation itself.
"""

from __future__ import annotations

import logging
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

logger = logging.getLogger(__name__)

__all__ = [
    "JOB_STATUS_CANCELLED",
    "JOB_STATUS_COMPLETED",
    "JOB_STATUS_FAILED",
    "JOB_STATUS_PENDING",
    "JOB_STATUS_RUNNING",
    "JobNotFoundError",
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
    "update_job_if_not_cancelled",
]


class JobNotFoundError(ValueError):
    """Raised by ``_guarded_transition`` when ``job_id`` does not exist.

    A distinct type (rather than a bare ``ValueError``) so callers — notably
    the Temporal workflow — can single it out as non-retryable: a missing job
    is a broken precondition that will not resolve itself on retry, unlike a
    transient network/service error. Subclasses ``ValueError`` so existing
    generic ``except ValueError``/``pytest.raises(ValueError, ...)`` callers
    keep working unchanged.
    """


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
update_job_if_not_cancelled = _store.update_job_if_not_cancelled
list_jobs = _store.list_jobs
cancel_job = _store.cancel_job
is_job_cancelled = _store.is_job_cancelled
delete_job = _store.delete_job
mark_all_running_jobs_failed = _store.mark_all_running_jobs_failed


def _guarded_transition(job_id: str, status: str, **extra_fields) -> bool:
    """Atomically write ``status`` (+ ``extra_fields``) to ``job_id`` unless
    already cancelled.

    The single atomic primitive behind ``begin_job``/``mark_completed``/
    ``mark_failed`` — every RUNNING/COMPLETED/FAILED transition in this team
    goes through this one function.

    Preconditions:
        - ``job_id`` refers to an existing job row; ``status`` is one of the
          ``JOB_STATUS_*`` constants; ``extra_fields`` are additional fields to
          merge (e.g. ``result=``/``error=``).
    Postconditions:
        - Returns False and makes no write if the job is already cancelled (a
          cancelled run is terminal — never overwritten with another status).
        - Otherwise writes ``status``/``extra_fields`` and returns True.
        - Atomic: the cancelled-check and the status-write happen in one
          server-side conditional update (``update_job_if_not_cancelled``), so a
          cancel landing between a caller's decision and this write can never be
          silently overwritten.
        - Raises ``JobNotFoundError`` if ``job_id`` does not exist — the
          primitive's tri-state return (True/False/None) tells "missing" apart
          from "cancelled" in the very same call, so this needs no supplementary
          read: a missing job is a broken precondition, not a legitimate
          cancellation, and is surfaced as such.
    """
    result = update_job_if_not_cancelled(job_id, status=status, **extra_fields)
    if result is None:
        logger.warning(
            "_guarded_transition: job %s does not exist — cannot transition to %s",
            job_id,
            status,
        )
        raise JobNotFoundError(
            f"_guarded_transition: job {job_id!r} does not exist (cannot transition to {status!r})"
        )
    if not result:
        logger.debug("Skipping transition to %s for job %s — already cancelled", status, job_id)
        return False
    return True


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
        - Otherwise writes status=RUNNING and returns True. Atomic — see
          ``_guarded_transition``.
        - Raises ``JobNotFoundError`` if ``job_id`` does not exist (precondition
          violation) — see ``_guarded_transition``.
    """
    return _guarded_transition(job_id, JOB_STATUS_RUNNING)


def mark_completed(job_id: str, result: dict) -> bool:
    """Mark ``job_id`` COMPLETED with ``result`` unless it was already cancelled.

    Preconditions:
        - ``job_id`` refers to an existing job row; ``result`` is a JSON-serializable
          mapping (e.g. ``TeamOutput.model_dump()``).
    Postconditions:
        - Returns False and makes no write if the job is already cancelled (a
          cancelled run is terminal, not a completion).
        - Otherwise writes status=COMPLETED with ``result`` and returns True.
          Atomic — see ``_guarded_transition``.
        - Raises ``JobNotFoundError`` if ``job_id`` does not exist (precondition
          violation) — see ``_guarded_transition``.
    """
    return _guarded_transition(job_id, JOB_STATUS_COMPLETED, result=result)


def mark_failed(job_id: str, error: str) -> bool:
    """Mark ``job_id`` FAILED with ``error`` unless it was already cancelled.

    Preconditions:
        - ``job_id`` refers to an existing job row; ``error`` is a short message.
    Postconditions:
        - Returns False and makes no write if the job is already cancelled (a
          cancelled run is terminal, not a failure).
        - Otherwise writes status=FAILED with ``error`` and returns True. Atomic
          — see ``_guarded_transition``.
        - Raises ``JobNotFoundError`` if ``job_id`` does not exist (precondition
          violation) — see ``_guarded_transition``.
    """
    return _guarded_transition(job_id, JOB_STATUS_FAILED, error=error)
