"""HTTP client for the central job service.

``JobServiceClient`` is the single Python entry point every agent team uses to
read/write job state.  It speaks HTTP to the ``khala-job-service`` container
defined in ``docker/docker-compose.yml``.

``JOB_SERVICE_URL`` is **required**.  The client raises ``RuntimeError`` at
construction time if it is not configured.  For local dev start the service
with ``docker compose -f docker/docker-compose.yml up -d postgres job-service``
and export ``JOB_SERVICE_URL=http://localhost:8085``.  Pytest spins up an
in-process job service automatically via ``backend/conftest.py``.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

# Re-export status constants so teams can import from here
JOB_STATUS_PENDING = "pending"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_COMPLETED = "completed"
JOB_STATUS_FAILED = "failed"
JOB_STATUS_CANCELLED = "cancelled"
JOB_STATUS_INTERRUPTED = "interrupted"


_MISSING_URL_MSG = (
    "JOB_SERVICE_URL is not set. Start the job service "
    "(`docker compose -f docker/docker-compose.yml up -d postgres job-service`) "
    "and export JOB_SERVICE_URL=http://localhost:8085, "
    "or run via pytest which provisions one in-process."
)


def _default_base_url() -> str:
    return os.environ.get("JOB_SERVICE_URL", "")


class JobServiceClient:
    """HTTP client for the central job service."""

    def __init__(self, team: str, base_url: str | None = None) -> None:
        self.team = team
        # Explicit base_url wins; otherwise resolve from JOB_SERVICE_URL each
        # request via the ``_base_url`` property below.  This matters because
        # team modules build module-level ``JobServiceClient(team=…)`` at import
        # time — pinning the env value at construction would freeze whatever
        # placeholder pytest's conftest had set before the integration fixture
        # swapped in the real URL.
        self._explicit_base_url: str | None = base_url.rstrip("/") if base_url else None
        if not self._explicit_base_url and not _default_base_url():
            raise RuntimeError(_MISSING_URL_MSG)

    @property
    def _base_url(self) -> str:
        if self._explicit_base_url is not None:
            return self._explicit_base_url
        url = _default_base_url()
        if not url:
            raise RuntimeError(_MISSING_URL_MSG)
        return url.rstrip("/")

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    def _request(
        self,
        method: str,
        url: str,
        *,
        timeout: float = 30.0,
        max_retries: int = 3,
        **kwargs: Any,
    ) -> httpx.Response:
        """Execute an HTTP request with retry on transient errors."""
        delays = [0.5, 1.0, 2.0]
        last_exc: Exception | None = None
        total_attempts = max_retries + 1
        for attempt in range(total_attempts):
            try:
                with httpx.Client(timeout=timeout) as client:
                    resp = client.request(method, url, **kwargs)
                    resp.raise_for_status()
                    return resp
            except (
                httpx.ConnectError,
                httpx.ReadTimeout,
                httpx.WriteTimeout,
                httpx.PoolTimeout,
            ) as exc:
                last_exc = exc
                if attempt < max_retries:
                    delay = delays[min(attempt, len(delays) - 1)]
                    time.sleep(delay)
                    continue
                raise
            except httpx.HTTPStatusError:
                raise
        raise last_exc  # type: ignore[misc]

    # ------------------------------------------------------------------
    # Core CRUD
    # ------------------------------------------------------------------

    def create_job(self, job_id: str, *, status: str = JOB_STATUS_PENDING, **fields: Any) -> None:
        self._request(
            "POST",
            self._url(f"/jobs/{self.team}"),
            json={"job_id": job_id, "status": status, "fields": fields},
        )

    def replace_job(self, job_id: str, payload: Dict[str, Any]) -> None:
        self._request(
            "POST",
            self._url(f"/jobs/{self.team}/{job_id}/replace"),
            json={"payload": payload},
        )

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        resp = self._request("GET", self._url(f"/jobs/{self.team}/{job_id}"))
        return resp.json().get("job")

    def delete_job(self, job_id: str) -> bool:
        resp = self._request("DELETE", self._url(f"/jobs/{self.team}/{job_id}"))
        return resp.json().get("deleted", False)

    def list_jobs(self, *, statuses: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        params = {}
        if statuses:
            params["statuses"] = statuses
        resp = self._request("GET", self._url(f"/jobs/{self.team}"), params=params)
        return resp.json().get("jobs", [])

    def update_job(self, job_id: str, *, heartbeat: bool = True, **fields: Any) -> None:
        self._request(
            "PATCH",
            self._url(f"/jobs/{self.team}/{job_id}"),
            json={"heartbeat": heartbeat, "fields": fields},
        )

    def append_event(
        self,
        job_id: str,
        *,
        action: str,
        outcome: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        status: Optional[str] = None,
    ) -> None:
        self._request(
            "POST",
            self._url(f"/jobs/{self.team}/{job_id}/event"),
            json={"action": action, "outcome": outcome, "details": details, "status": status},
        )

    def mark_stale_active_jobs_failed(
        self,
        *,
        stale_after_seconds: float,
        reason: str,
        waiting_field: str = "waiting_for_answers",
    ) -> List[str]:
        resp = self._request(
            "POST",
            self._url(f"/jobs/{self.team}/mark-stale-failed"),
            json={
                "stale_after_seconds": stale_after_seconds,
                "reason": reason,
                "waiting_field": waiting_field,
            },
        )
        return resp.json().get("failed_job_ids", [])

    def mark_all_active_jobs_failed(
        self,
        reason: str,
        *,
        http_timeout: float = 30.0,
        http_max_retries: int = 3,
    ) -> List[str]:
        """Mark all active (pending/running) jobs as failed (e.g. on server shutdown).

        Skips jobs in a waiting state (waiting_for_answers, waiting_for_title_selection,
        waiting_for_story_input).
        """
        resp = self._request(
            "POST",
            self._url(f"/jobs/{self.team}/mark-all-running-failed"),
            json={"reason": reason},
            timeout=http_timeout,
            max_retries=http_max_retries,
        )
        return resp.json().get("failed_job_ids", [])

    def mark_all_active_jobs_interrupted(
        self,
        reason: str,
        *,
        http_timeout: float = 30.0,
        http_max_retries: int = 3,
    ) -> List[str]:
        """Mark all active (pending/running) jobs as interrupted due to service shutdown."""
        resp = self._request(
            "POST",
            self._url(f"/jobs/{self.team}/mark-all-running-interrupted"),
            json={"reason": reason},
            timeout=http_timeout,
            max_retries=http_max_retries,
        )
        return resp.json().get("interrupted_job_ids", [])

    # ------------------------------------------------------------------
    # Atomic patch helpers
    # ------------------------------------------------------------------

    def merge_nested(self, job_id: str, path: str, data: Dict[str, Any]) -> None:
        """Merge *data* into a nested dict at *path* (dot-separated)."""
        self._request(
            "POST",
            self._url(f"/jobs/{self.team}/{job_id}/apply"),
            json={"merge_nested": {path: data}},
        )

    def append_to_list(self, job_id: str, field: str, items: List[Any]) -> None:
        """Append *items* to the list stored at *field*."""
        self._request(
            "POST",
            self._url(f"/jobs/{self.team}/{job_id}/apply"),
            json={"append_to": {field: items}},
        )

    def atomic_update(
        self,
        job_id: str,
        *,
        merge_fields: Optional[Dict[str, Any]] = None,
        merge_nested: Optional[Dict[str, Any]] = None,
        append_to: Optional[Dict[str, List[Any]]] = None,
        increment: Optional[Dict[str, int]] = None,
    ) -> None:
        """Perform an atomic batch of merge + append + increment operations."""
        self._request(
            "POST",
            self._url(f"/jobs/{self.team}/{job_id}/apply"),
            json={
                "merge_fields": merge_fields,
                "merge_nested": merge_nested,
                "append_to": append_to,
                "increment": increment,
            },
        )

    def increment_field(self, job_id: str, field: str, delta: int = 1) -> None:
        """Atomically increment an integer field by *delta*."""
        self.atomic_update(job_id, increment={field: delta})

    def heartbeat(self, job_id: str) -> None:
        """Touch last_heartbeat_at for a job."""
        self._request("POST", self._url(f"/jobs/{self.team}/{job_id}/heartbeat"))


# ---------------------------------------------------------------------------
# Stale job monitor
# ---------------------------------------------------------------------------


def start_stale_job_monitor(
    client: JobServiceClient,
    *,
    interval_seconds: float,
    stale_after_seconds: float,
    reason: str,
) -> threading.Event:
    """Start a background thread that periodically marks stale jobs as failed."""
    stop_event = threading.Event()

    def _run() -> None:
        while not stop_event.is_set():
            try:
                client.mark_stale_active_jobs_failed(
                    stale_after_seconds=stale_after_seconds,
                    reason=reason,
                )
            except Exception as exc:
                logger.warning("stale job monitor error (%s): %s", client.team, exc)
            stop_event.wait(interval_seconds)

    thread = threading.Thread(target=_run, name=f"{client.team}-stale-job-monitor", daemon=True)
    thread.start()
    return stop_event


# ---------------------------------------------------------------------------
# Shared validation helper for resume/restart endpoints
# ---------------------------------------------------------------------------

# Standard status sets for resume/restart gating.
RESUMABLE_STATUSES: frozenset[str] = frozenset(
    {
        JOB_STATUS_PENDING,
        JOB_STATUS_RUNNING,
        JOB_STATUS_FAILED,
        JOB_STATUS_INTERRUPTED,
        "agent_crash",
    }
)
RESTARTABLE_STATUSES: frozenset[str] = frozenset(
    {
        JOB_STATUS_COMPLETED,
        JOB_STATUS_FAILED,
        JOB_STATUS_CANCELLED,
        JOB_STATUS_INTERRUPTED,
        "agent_crash",
    }
)


def validate_job_for_action(
    job_data: Optional[Dict[str, Any]],
    job_id: str,
    allowed_statuses: frozenset[str],
    action_label: str = "action",
) -> Dict[str, Any]:
    """Validate a job exists and is in an allowed status.

    Raises ``ValueError`` with a human-readable message on failure.
    The caller should catch this and convert to an ``HTTPException``.

    Returns the job data dict on success.
    """
    if not job_data:
        raise ValueError(f"Job {job_id} not found")
    status = job_data.get("status", JOB_STATUS_PENDING)
    if status not in allowed_statuses:
        raise ValueError(f"Job cannot be {action_label} (status={status}).")
    return job_data


# ---------------------------------------------------------------------------
# Base job store — eliminates duplicated CRUD wrappers across teams
# ---------------------------------------------------------------------------


class BaseJobStore:
    """Shared job store operations that all teams duplicate.

    Subclass and set ``team`` to get create/get/update/delete/list/reset
    for free.  Override or add team-specific methods as needed.

    Usage::

        class BlogJobStore(BaseJobStore):
            team = "blogging_team"

            def submit_title_selection(self, job_id, title): ...
    """

    team: str = ""  # Override in subclass

    def _client(self) -> JobServiceClient:
        return JobServiceClient(team=self.team)

    def create_job(self, job_id: str, *, status: str = JOB_STATUS_PENDING, **fields: Any) -> None:
        self._client().create_job(job_id, status=status, **fields)

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        return self._client().get_job(job_id)

    def update_job(self, job_id: str, **kwargs: Any) -> None:
        self._client().update_job(job_id, **kwargs)

    def delete_job(self, job_id: str) -> bool:
        return self._client().delete_job(job_id)

    def list_jobs(self, *, running_only: bool = False) -> List[Dict[str, Any]]:
        statuses = [JOB_STATUS_PENDING, JOB_STATUS_RUNNING] if running_only else None
        return self._client().list_jobs(statuses=statuses) or []

    def mark_job_running(self, job_id: str) -> None:
        self.update_job(job_id, status=JOB_STATUS_RUNNING, started_at=_now_iso())

    def mark_job_completed(self, job_id: str, **extra: Any) -> None:
        self.update_job(
            job_id, status=JOB_STATUS_COMPLETED, progress=100, completed_at=_now_iso(), **extra
        )

    def mark_job_failed(self, job_id: str, error: str) -> None:
        self.update_job(job_id, status=JOB_STATUS_FAILED, error=error)

    def mark_all_running_jobs_failed(self, reason: str) -> List[str]:
        return self._client().mark_all_active_jobs_failed(reason)

    def reset_job(self, job_id: str) -> None:
        """Reset a job to initial state for restart (preserves input params)."""
        self.update_job(
            job_id,
            status=JOB_STATUS_PENDING,
            progress=0,
            error=None,
            current_phase=None,
            status_text=None,
        )


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(tz=timezone.utc).isoformat()
