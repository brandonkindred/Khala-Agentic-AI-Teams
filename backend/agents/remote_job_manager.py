from __future__ import annotations

import copy
import logging
import os
from typing import Any, Callable, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 60.0
_MAX_APPLY_RETRIES = 8


def _base_url() -> str:
    return os.getenv("JOB_SERVICE_URL", "").strip().rstrip("/")


def _headers() -> Dict[str, str]:
    h: Dict[str, str] = {}
    key = os.getenv("JOB_SERVICE_API_KEY", "").strip()
    if key:
        h["X-Job-Service-Key"] = key
    return h


class RemoteJobManager:
    """HTTP client for the central job microservice (same responsibilities as CentralJobManager)."""

    def __init__(self, team: str, cache_dir: str | Any = ".agent_cache") -> None:
        self.team = team
        self.cache_dir = cache_dir  # unused; kept for API parity with CentralJobManager
        self._client = httpx.Client(
            base_url=_base_url(),
            headers=_headers(),
            timeout=_DEFAULT_TIMEOUT,
        )

    def close(self) -> None:
        self._client.close()

    def _url(self, job_id: str, suffix: str = "") -> str:
        return f"/v1/jobs/{self.team}/{job_id}{suffix}"

    def create_job(self, job_id: str, *, status: str = "pending", **fields: Any) -> None:
        now_fields = dict(fields)
        now_fields.setdefault("job_id", job_id)
        now_fields.setdefault("status", status)
        r = self._client.put(self._url(job_id), json=now_fields)
        if r.status_code == 409:
            r = self._client.put(
                self._url(job_id), json=now_fields, params={"unconditional": "true"}
            )
        r.raise_for_status()

    def replace_job(self, job_id: str, payload: Dict[str, Any]) -> None:
        r = self._client.put(self._url(job_id), json=payload, params={"unconditional": "true"})
        r.raise_for_status()

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        r = self._client.get(self._url(job_id))
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return copy.deepcopy(r.json())

    def delete_job(self, job_id: str) -> bool:
        r = self._client.delete(self._url(job_id))
        if r.status_code == 404:
            return False
        r.raise_for_status()
        return True

    def list_jobs(self, *, statuses: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        params: Dict[str, str] = {"team": self.team}
        if statuses:
            params["status"] = ",".join(statuses)
        r = self._client.get("/v1/jobs", params=params)
        r.raise_for_status()
        jobs = r.json().get("jobs") or []
        return [copy.deepcopy(j) for j in jobs]

    def update_job(self, job_id: str, *, heartbeat: bool = True, **fields: Any) -> None:
        body = {"fields": dict(fields), "heartbeat": heartbeat, "expected_version": None}
        r = self._client.patch(self._url(job_id), json=body)
        r.raise_for_status()

    def apply_to_job(self, job_id: str, fn: Callable[[Dict[str, Any]], None]) -> None:
        for attempt in range(_MAX_APPLY_RETRIES):
            r = self._client.get(self._url(job_id))
            if r.status_code == 404:
                return
            r.raise_for_status()
            data = r.json()
            ver_hdr = r.headers.get("X-Job-Version")
            version = int(ver_hdr) if ver_hdr else None
            fn(data)
            data.pop("_version", None)
            headers = {}
            if version is not None:
                headers["If-Match"] = str(version)
            put = self._client.put(self._url(job_id), json=data, headers=headers)
            if put.status_code == 409:
                continue
            put.raise_for_status()
            return
        logger.warning("apply_to_job exhausted retries for %s/%s", self.team, job_id)

    def append_event(
        self,
        job_id: str,
        *,
        action: str,
        outcome: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        status: Optional[str] = None,
    ) -> None:
        body: Dict[str, Any] = {
            "action": action,
            "outcome": outcome,
            "details": details or {},
            "status": status,
            "expected_version": None,
        }
        r = self._client.post(self._url(job_id, "/events"), json=body)
        r.raise_for_status()

    def mark_stale_active_jobs_failed(
        self,
        *,
        stale_after_seconds: float,
        reason: str,
        waiting_field: str = "waiting_for_answers",
    ) -> List[str]:
        if stale_after_seconds > 0:
            return []
        r = self._client.post(
            f"/v1/jobs/{self.team}/fail-active",
            json={"reason": reason},
        )
        r.raise_for_status()
        data = r.json()
        return list(data.get("job_ids") or [])

    def send_heartbeat(self, job_id: str) -> None:
        r = self._client.post(self._url(job_id, "/heartbeat"))
        if r.status_code == 404:
            return
        r.raise_for_status()
