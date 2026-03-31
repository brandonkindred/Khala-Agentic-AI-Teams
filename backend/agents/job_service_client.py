"""HTTP client for the containerized job service.

This module provides ``JobServiceClient`` — a drop-in replacement for
``CentralJobManager`` that talks to the job service over HTTP.  All agent
teams import this instead of ``CentralJobManager`` directly.

When ``JOB_SERVICE_URL`` is **not** set the client falls back to a local
``CentralJobManager`` instance so that ``make run`` and ``pytest`` work
without requiring the job service container.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

_ACTIVE_STATUSES = {"pending", "running"}

# Re-export status constants so teams can import from here
JOB_STATUS_PENDING = "pending"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_COMPLETED = "completed"
JOB_STATUS_FAILED = "failed"
JOB_STATUS_CANCELLED = "cancelled"
JOB_STATUS_INTERRUPTED = "interrupted"


def _default_base_url() -> str:
    return os.environ.get("JOB_SERVICE_URL", "")


def _is_remote_mode() -> bool:
    return bool(_default_base_url())


class JobServiceClient:
    """HTTP client for the containerized job service.

    Drop-in replacement for ``CentralJobManager``.  When ``JOB_SERVICE_URL``
    is set, communicates with the job service over HTTP.  Otherwise delegates
    to a local ``CentralJobManager`` for backwards-compatible local dev.
    """

    def __init__(
        self, team: str, base_url: str | None = None, cache_dir: str | None = None
    ) -> None:
        self.team = team
        self._base_url = base_url or _default_base_url()
        self._cache_dir = cache_dir
        self._local: Any = None  # Lazy-init CentralJobManager for local fallback

    @property
    def _is_remote(self) -> bool:
        return bool(self._base_url)

    def _get_local(self):
        """Lazy-create a local CentralJobManager for non-Docker usage."""
        if self._local is None:
            from shared_job_management import CentralJobManager

            cache_dir = self._cache_dir or os.getenv("AGENT_CACHE", ".agent_cache")
            self._local = CentralJobManager(team=self.team, cache_dir=cache_dir)
        return self._local

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
        if not self._is_remote:
            self._get_local().create_job(job_id, status=status, **fields)
            return
        self._request(
            "POST",
            self._url(f"/jobs/{self.team}"),
            json={"job_id": job_id, "status": status, "fields": fields},
        )

    def replace_job(self, job_id: str, payload: Dict[str, Any]) -> None:
        if not self._is_remote:
            self._get_local().replace_job(job_id, payload)
            return
        self._request(
            "POST",
            self._url(f"/jobs/{self.team}/{job_id}/replace"),
            json={"payload": payload},
        )

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        if not self._is_remote:
            return self._get_local().get_job(job_id)
        resp = self._request("GET", self._url(f"/jobs/{self.team}/{job_id}"))
        return resp.json().get("job")

    def delete_job(self, job_id: str) -> bool:
        if not self._is_remote:
            return self._get_local().delete_job(job_id)
        resp = self._request("DELETE", self._url(f"/jobs/{self.team}/{job_id}"))
        return resp.json().get("deleted", False)

    def list_jobs(self, *, statuses: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        if not self._is_remote:
            return self._get_local().list_jobs(statuses=statuses)
        params = {}
        if statuses:
            params["statuses"] = statuses
        resp = self._request("GET", self._url(f"/jobs/{self.team}"), params=params)
        return resp.json().get("jobs", [])

    def update_job(self, job_id: str, *, heartbeat: bool = True, **fields: Any) -> None:
        if not self._is_remote:
            self._get_local().update_job(job_id, heartbeat=heartbeat, **fields)
            return
        self._request(
            "PATCH",
            self._url(f"/jobs/{self.team}/{job_id}"),
            json={"heartbeat": heartbeat, "fields": fields},
        )

    def apply_to_job(self, job_id: str, fn: Callable[[Dict[str, Any]], None]) -> None:
        """For local fallback only. Remote callers should use merge_nested/append_to_list/atomic_update."""
        if not self._is_remote:
            self._get_local().apply_to_job(job_id, fn)
            return
        raise NotImplementedError(
            "apply_to_job with callables is not supported over HTTP. "
            "Use merge_nested(), append_to_list(), or atomic_update() instead."
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
        if not self._is_remote:
            self._get_local().append_event(
                job_id, action=action, outcome=outcome, details=details, status=status
            )
            return
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
        if not self._is_remote:
            return self._get_local().mark_stale_active_jobs_failed(
                stale_after_seconds=stale_after_seconds, reason=reason, waiting_field=waiting_field
            )
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
        if not self._is_remote:
            local = self._get_local()
            failed: List[str] = []
            _waiting_fields = (
                "waiting_for_answers",
                "waiting_for_title_selection",
                "waiting_for_story_input",
            )
            for job in local.list_jobs(statuses=list(_ACTIVE_STATUSES)):
                if any(job.get(wf) for wf in _waiting_fields):
                    continue
                jid = job.get("job_id")
                if jid:
                    local.update_job(jid, status=JOB_STATUS_FAILED, error=reason)
                    failed.append(jid)
            return failed
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
        if not self._is_remote:
            local = self._get_local()
            interrupted: List[str] = []
            _waiting_fields = (
                "waiting_for_answers",
                "waiting_for_title_selection",
                "waiting_for_story_input",
            )
            for job in local.list_jobs(statuses=list(_ACTIVE_STATUSES)):
                if any(job.get(wf) for wf in _waiting_fields):
                    continue
                jid = job.get("job_id")
                if jid:
                    local.update_job(jid, status=JOB_STATUS_INTERRUPTED, error=reason)
                    interrupted.append(jid)
            return interrupted
        resp = self._request(
            "POST",
            self._url(f"/jobs/{self.team}/mark-all-running-interrupted"),
            json={"reason": reason},
            timeout=http_timeout,
            max_retries=http_max_retries,
        )
        return resp.json().get("interrupted_job_ids", [])

    # ------------------------------------------------------------------
    # Atomic patch helpers (HTTP-safe replacements for apply_to_job)
    # ------------------------------------------------------------------

    def merge_nested(self, job_id: str, path: str, data: Dict[str, Any]) -> None:
        """Merge *data* into a nested dict at *path* (dot-separated)."""
        if not self._is_remote:

            def _fn(d: Dict[str, Any]) -> None:
                parts = path.split(".")
                target = d
                for part in parts[:-1]:
                    target = target.setdefault(part, {})
                leaf = parts[-1]
                existing = target.get(leaf, {})
                if isinstance(existing, dict) and isinstance(data, dict):
                    existing.update(data)
                    target[leaf] = existing
                else:
                    target[leaf] = data

            self._get_local().apply_to_job(job_id, _fn)
            return
        self._request(
            "POST",
            self._url(f"/jobs/{self.team}/{job_id}/apply"),
            json={"merge_nested": {path: data}},
        )

    def append_to_list(self, job_id: str, field: str, items: List[Any]) -> None:
        """Append *items* to the list stored at *field*."""
        if not self._is_remote:

            def _fn(d: Dict[str, Any]) -> None:
                existing = d.get(field, [])
                if not isinstance(existing, list):
                    existing = []
                existing.extend(items)
                d[field] = existing

            self._get_local().apply_to_job(job_id, _fn)
            return
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
        if not self._is_remote:

            def _fn(d: Dict[str, Any]) -> None:
                if merge_fields:
                    d.update(merge_fields)
                if merge_nested:
                    for dotted_path, value in merge_nested.items():
                        parts = dotted_path.split(".")
                        target = d
                        for part in parts[:-1]:
                            target = target.setdefault(part, {})
                        leaf = parts[-1]
                        existing = target.get(leaf, {})
                        if isinstance(existing, dict) and isinstance(value, dict):
                            existing.update(value)
                            target[leaf] = existing
                        else:
                            target[leaf] = value
                if append_to:
                    for field_name, items in append_to.items():
                        existing_list = d.get(field_name, [])
                        if not isinstance(existing_list, list):
                            existing_list = []
                        existing_list.extend(items)
                        d[field_name] = existing_list
                if increment:
                    for field_name, delta in increment.items():
                        current = d.get(field_name, 0)
                        if not isinstance(current, (int, float)):
                            current = 0
                        d[field_name] = current + delta

            self._get_local().apply_to_job(job_id, _fn)
            return
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
        if not self._is_remote:
            self._get_local().update_job(job_id)
            return
        self._request("POST", self._url(f"/jobs/{self.team}/{job_id}/heartbeat"))


# ---------------------------------------------------------------------------
# Stale job monitor (remote-compatible)
# ---------------------------------------------------------------------------


def start_stale_job_monitor(
    client: JobServiceClient,
    *,
    interval_seconds: float,
    stale_after_seconds: float,
    reason: str,
) -> threading.Event:
    """Start a background thread that periodically marks stale jobs as failed.

    Works with both remote (HTTP) and local (file-backed) modes.
    """
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
