"""In-memory execution tracker with derived progress, loop, and timing metrics.

The module-level :data:`execution_tracker` is a process-wide singleton shared by
every SE job. To keep it from growing without bound over a long-lived server, both
of its collections are capped:

- ``_events`` is a bounded ``deque`` (it is appended on *every* task operation,
  including once per loop iteration, so it is the dominant growth source). An
  ``_events_evicted`` counter records how many were dropped off the front so the
  SSE stream's monotonic index (total events emitted, not buffer position) keeps
  pointing at the right place.
- ``_tasks`` is a capped ``OrderedDict`` with FIFO eviction. A single job's task
  set is far below the (generous) cap, so eviction only ever drops tasks left over
  from earlier completed jobs.

Caps are tunable via ``SE_EXECUTION_TRACKER_EVENT_CAP`` /
``SE_EXECUTION_TRACKER_TASK_CAP`` (defensive parse: garbage -> default, values
below the floor are clamped up).

Invariants:
    - ``len(_events) <= EVENT_CAP`` and ``len(_tasks) <= TASK_CAP`` at all times.
    - ``_events_evicted`` is monotonically non-decreasing and equals the number of
      events dropped off the front of ``_events`` since process start.
"""

from __future__ import annotations

import os
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock
from typing import Deque, List

_DEFAULT_EVENT_CAP = 5000
_DEFAULT_TASK_CAP = 5000
_MIN_CAP = 100


def _resolve_cap(env_name: str, default: int) -> int:
    """Return a positive cap from ``env_name``, defaulting + clamping defensively.

    Preconditions: ``default >= _MIN_CAP``.
    Postconditions: returns an int ``>= _MIN_CAP``; a missing/unparseable env value
        yields ``default``; a value below ``_MIN_CAP`` is clamped up. Never raises.
    """
    raw = os.environ.get(env_name)
    if not raw:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(_MIN_CAP, value)


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
        self._event_cap = _resolve_cap("SE_EXECUTION_TRACKER_EVENT_CAP", _DEFAULT_EVENT_CAP)
        self._task_cap = _resolve_cap("SE_EXECUTION_TRACKER_TASK_CAP", _DEFAULT_TASK_CAP)
        self._tasks: "OrderedDict[str, ExecutionTask]" = OrderedDict()
        self._events: Deque[dict] = deque(maxlen=self._event_cap)
        self._events_evicted = 0
        self._lock = Lock()

    def _emit(self, event_type: str, payload: dict) -> None:
        # When the bounded deque is full, the append drops one event off the front;
        # count it so events_since()/event_count stay aligned with the consumer's
        # monotonic (total-emitted) index.
        if len(self._events) == self._event_cap:
            self._events_evicted += 1
        self._events.append({"type": event_type, "timestamp": _iso(_utc_now()), "payload": payload})

    def _store_task(self, task_id: str, task: ExecutionTask) -> None:
        # FIFO eviction: a single job's task set stays well under the cap, so the
        # only entries dropped are leftovers from earlier completed jobs.
        if task_id not in self._tasks and len(self._tasks) >= self._task_cap:
            self._tasks.popitem(last=False)
        self._tasks[task_id] = task

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
                self._store_task(
                    task_id,
                    ExecutionTask(
                        task_id=task_id,
                        title=title,
                        assigned_agent=assigned_agent,
                        dependencies=dependencies or [],
                    ),
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
                # Total events ever emitted (incl. evicted), so a consumer's index
                # stays meaningful even after the buffer wraps.
                "event_count": self._events_evicted + len(self._events),
            }

    def events_since(self, index: int) -> List[dict]:
        """Return events emitted at total-position ``index`` and later.

        Preconditions: ``index >= 0`` (a monotonic count of events the caller has
            already consumed, as produced by ``snapshot()['event_count']`` / prior
            calls).
        Postconditions: returns the still-buffered events from total-position
            ``index`` onward. If ``index`` predates the eviction window, the caller
            fell behind and gets everything still buffered (no error). Never raises.
        """
        with self._lock:
            start = max(0, index - self._events_evicted)
            return list(self._events)[start:]

    def reset(self) -> None:
        """Clear all tracked tasks and events.

        Postconditions: ``_tasks`` and ``_events`` are empty and ``_events_evicted``
            is 0. Not called automatically (the singleton is shared across possibly
            concurrent jobs); provided for tests and explicit lifecycle control.
        """
        with self._lock:
            self._tasks.clear()
            self._events.clear()
            self._events_evicted = 0


execution_tracker = ExecutionTracker()
