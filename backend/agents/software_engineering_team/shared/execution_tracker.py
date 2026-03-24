"""In-memory execution tracker with derived progress, loop, and timing metrics."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock
from typing import Dict, List


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(ts: datetime | None) -> str | None:
    return ts.isoformat() if ts else None


@dataclass
class ExecutionTask:
    task_id: str
    title: str
    assigned_agent: str
    status: str = "pending"
    dependencies: List[str] = field(default_factory=list)
    percent_complete: float = 0.0
    loop_counts: List[int] = field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def to_dict(self) -> dict:
        loop_min = min(self.loop_counts) if self.loop_counts else 0
        loop_max = max(self.loop_counts) if self.loop_counts else 0
        loop_avg = (sum(self.loop_counts) / len(self.loop_counts)) if self.loop_counts else 0.0
        duration_seconds = None
        if self.started_at and self.finished_at:
            duration_seconds = int((self.finished_at - self.started_at).total_seconds())
        return {
            "task_id": self.task_id,
            "title": self.title,
            "assigned_agent": self.assigned_agent,
            "status": self.status,
            "dependencies": self.dependencies,
            "percent_complete": round(self.percent_complete, 2),
            "loop_count_min": loop_min,
            "loop_count_max": loop_max,
            "loop_count_avg": round(loop_avg, 2),
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
            "duration_seconds": duration_seconds,
        }


class ExecutionTracker:
    def __init__(self) -> None:
        self._tasks: Dict[str, ExecutionTask] = {}
        self._events: List[dict] = []
        self._lock = Lock()

    def _emit(self, event_type: str, payload: dict) -> None:
        self._events.append({"type": event_type, "timestamp": _iso(_utc_now()), "payload": payload})

    def upsert_task(
        self, task_id: str, title: str, assigned_agent: str, dependencies: List[str] | None = None
    ) -> None:
        with self._lock:
            existing = self._tasks.get(task_id)
            if existing:
                existing.title = title or existing.title
                existing.assigned_agent = assigned_agent or existing.assigned_agent
                if dependencies is not None:
                    existing.dependencies = dependencies
            else:
                self._tasks[task_id] = ExecutionTask(
                    task_id=task_id,
                    title=title,
                    assigned_agent=assigned_agent,
                    dependencies=dependencies or [],
                )
            self._emit("task_upserted", {"task_id": task_id})

    def start_task(self, task_id: str) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return
            task.status = "in_progress"
            task.started_at = task.started_at or _utc_now()
            task.percent_complete = max(task.percent_complete, 5.0)
            self._emit("task_started", {"task_id": task_id})

    def update_progress(self, task_id: str, percent_complete: float) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return
            task.percent_complete = max(0.0, min(100.0, percent_complete))
            if task.percent_complete >= 100:
                task.status = "done"
                task.finished_at = task.finished_at or _utc_now()
            self._emit(
                "task_progress", {"task_id": task_id, "percent_complete": task.percent_complete}
            )

    def observe_loop(self, task_id: str, loop_count: int) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return
            task.loop_counts.append(max(0, loop_count))
            self._emit("task_loop_observed", {"task_id": task_id, "loop_count": loop_count})

    def finish_task(self, task_id: str, *, blocked: bool = False) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return
            task.status = "blocked" if blocked else "done"
            task.percent_complete = 100.0 if not blocked else task.percent_complete
            task.started_at = task.started_at or _utc_now()
            task.finished_at = _utc_now()
            self._emit("task_finished" if not blocked else "task_blocked", {"task_id": task_id})

    def snapshot(self) -> dict:
        with self._lock:
            tasks = [t.to_dict() for t in self._tasks.values()]
            total = len(tasks)
            done = sum(1 for t in tasks if t["status"] == "done")
            percent = 0.0 if total == 0 else round((done / total) * 100.0, 2)
            return {
                "plan_progress_percent": percent,
                "tasks": sorted(tasks, key=lambda t: t["task_id"]),
                "event_count": len(self._events),
            }

    def events_since(self, index: int) -> List[dict]:
        with self._lock:
            return self._events[index:]


execution_tracker = ExecutionTracker()
