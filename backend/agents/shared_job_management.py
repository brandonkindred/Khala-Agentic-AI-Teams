from __future__ import annotations

import copy
import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

JOB_STATUS_PENDING = "pending"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_COMPLETED = "completed"
JOB_STATUS_FAILED = "failed"
JOB_STATUS_CANCELLED = "cancelled"

_ACTIVE_STATUSES = {JOB_STATUS_PENDING, JOB_STATUS_RUNNING}


class CentralJobManager:
    """File-backed job management shared by all teams."""

    def __init__(self, team: str, cache_dir: str | Path = ".agent_cache") -> None:
        self.team = team
        self.cache_dir = Path(cache_dir)
        self._lock = threading.Lock()

    def _jobs_dir(self) -> Path:
        path = self.cache_dir / self.team / "jobs"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _job_file(self, job_id: str) -> Path:
        return self._jobs_dir() / f"{job_id}.json"

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _read(path: Path) -> Optional[Dict[str, Any]]:
        if not path.exists():
            return None
        for _ in range(2):
            try:
                raw = path.read_text(encoding="utf-8")
                if not raw.strip():
                    time.sleep(0.05)
                    continue
                return json.loads(raw)
            except Exception:
                time.sleep(0.05)
        return None

    @staticmethod
    def _write(path: Path, payload: Dict[str, Any]) -> None:
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def create_job(self, job_id: str, *, status: str = JOB_STATUS_PENDING, **fields: Any) -> None:
        now = self._now()
        payload: Dict[str, Any] = {
            "job_id": job_id,
            "team": self.team,
            "status": status,
            "created_at": now,
            "updated_at": now,
            "last_heartbeat_at": now,
            "events": [],
        }
        payload.update(fields)
        with self._lock:
            self._write(self._job_file(job_id), payload)

    def replace_job(self, job_id: str, payload: Dict[str, Any]) -> None:
        """Overwrite the job file with the given payload. Use for reset (same job_id, fresh state)."""
        with self._lock:
            self._write(self._job_file(job_id), payload)

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            data = self._read(self._job_file(job_id))
            return copy.deepcopy(data) if data else None

    def delete_job(self, job_id: str) -> bool:
        """Remove the job file from the store. Returns True if removed, False if not found."""
        with self._lock:
            path = self._job_file(job_id)
            if not path.exists():
                return False
            path.unlink()
            return True

    def list_jobs(self, *, statuses: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        with self._lock:
            for path in self._jobs_dir().glob("*.json"):
                data = self._read(path)
                if not data:
                    continue
                if statuses and data.get("status") not in statuses:
                    continue
                result.append(copy.deepcopy(data))
        result.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        return result

    def update_job(self, job_id: str, *, heartbeat: bool = True, **fields: Any) -> None:
        with self._lock:
            path = self._job_file(job_id)
            data = self._read(path) or {}
            data.update(fields)
            now = self._now()
            data["updated_at"] = now
            if heartbeat:
                data["last_heartbeat_at"] = now
            self._write(path, data)

    def apply_to_job(self, job_id: str, fn: Callable[[Dict[str, Any]], None]) -> None:
        """Atomically read job, call fn(data), write back. No-op if job does not exist."""
        with self._lock:
            path = self._job_file(job_id)
            data = self._read(path)
            if data is None:
                return
            fn(data)
            now = self._now()
            data["updated_at"] = now
            data["last_heartbeat_at"] = now
            self._write(path, data)

    def append_event(
        self,
        job_id: str,
        *,
        action: str,
        outcome: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        status: Optional[str] = None,
    ) -> None:
        with self._lock:
            path = self._job_file(job_id)
            data = self._read(path) or {}
            events = data.get("events", [])
            events.append(
                {
                    "timestamp": self._now(),
                    "action": action,
                    "outcome": outcome,
                    "details": details or {},
                }
            )
            data["events"] = events
            if status is not None:
                data["status"] = status
            data["updated_at"] = self._now()
            data["last_heartbeat_at"] = self._now()
            self._write(path, data)

    def mark_stale_active_jobs_failed(
        self,
        *,
        stale_after_seconds: float,
        reason: str,
        waiting_field: str = "waiting_for_answers",
    ) -> List[str]:
        failed_job_ids: List[str] = []
        now = datetime.now(timezone.utc)
        with self._lock:
            for path in self._jobs_dir().glob("*.json"):
                data = self._read(path)
                if not data:
                    continue
                status = data.get("status")
                if status not in _ACTIVE_STATUSES:
                    continue
                if data.get(waiting_field):
                    continue
                hb_raw = (
                    data.get("last_heartbeat_at")
                    or data.get("updated_at")
                    or data.get("created_at")
                )
                try:
                    hb = datetime.fromisoformat(str(hb_raw))
                except Exception:
                    hb = now
                if (now - hb).total_seconds() <= stale_after_seconds:
                    continue
                data["status"] = JOB_STATUS_FAILED
                data["error"] = reason
                data["updated_at"] = self._now()
                self._write(path, data)
                failed_job_ids.append(str(data.get("job_id", path.stem)))
        if failed_job_ids:
            logger.warning("Marked stale jobs failed for team %s: %s", self.team, failed_job_ids)
        return failed_job_ids


def start_stale_job_monitor(
    manager: CentralJobManager,
    *,
    interval_seconds: float,
    stale_after_seconds: float,
    reason: str,
) -> threading.Event:
    stop_event = threading.Event()

    def _run() -> None:
        while not stop_event.is_set():
            try:
                manager.mark_stale_active_jobs_failed(
                    stale_after_seconds=stale_after_seconds,
                    reason=reason,
                )
            except Exception as exc:
                logger.warning("stale job monitor error (%s): %s", manager.team, exc)
            stop_event.wait(interval_seconds)

    thread = threading.Thread(target=_run, name=f"{manager.team}-stale-job-monitor", daemon=True)
    thread.start()
    return stop_event
