"""
Task Graph service: per-job store of tasks and dependencies.
Enforces one active task per agent and next task only after merge.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from coding_team.models import Task, TaskStatus

logger = logging.getLogger(__name__)


class TaskGraphService:
    """
    In-memory Task Graph for a single job. Tracks tasks and which agent has which (non-merged) task.
    Assign: allowed only if agent has no current task or current task is merged, and task deps satisfied.
    """

    def __init__(
        self,
        job_id: str,
        persist_callback: Optional[Callable[[], None]] = None,
    ) -> None:
        self.job_id = job_id
        self._tasks: Dict[str, Task] = {}
        self._agent_to_task: Dict[str, str] = {}  # agent_id -> task_id (only non-merged)
        self._persist = persist_callback

    def add_task(
        self,
        task_id: str,
        title: str = "",
        description: str = "",
        dependencies: Optional[List[str]] = None,
        acceptance_criteria: Optional[List[str]] = None,
        out_of_scope: str = "",
        priority: str = "medium",
        subtasks: Optional[List[Any]] = None,
    ) -> Task:
        """Add a task. Id must be unique."""
        if task_id in self._tasks:
            raise ValueError(f"Task {task_id} already exists")
        task = Task(
            id=task_id,
            title=title or task_id,
            description=description,
            dependencies=dependencies or [],
            status=TaskStatus.TO_DO,
            acceptance_criteria=acceptance_criteria or [],
            out_of_scope=out_of_scope,
            priority=priority,
            subtasks=subtasks or [],
        )
        self._tasks[task_id] = task
        self._maybe_persist()
        return task

    def update_task(
        self,
        task_id: str,
        status: Optional[TaskStatus] = None,
        assigned_agent_id: Optional[str] = None,
        feature_branch: Optional[str] = None,
        merged_at: Optional[datetime] = None,
        **kwargs: Any,
    ) -> Optional[Task]:
        """Update task fields. Returns the task if found."""
        task = self._tasks.get(task_id)
        if not task:
            return None
        if status is not None:
            task.status = status
        if assigned_agent_id is not None:
            task.assigned_agent_id = assigned_agent_id
        if feature_branch is not None:
            task.feature_branch = feature_branch
        if merged_at is not None:
            task.merged_at = merged_at
        for k, v in kwargs.items():
            if hasattr(task, k):
                setattr(task, k, v)
        self._maybe_persist()
        return task

    def get_tasks(self) -> List[Task]:
        """Return all tasks (copy)."""
        return list(self._tasks.values())

    def get_task(self, task_id: str) -> Optional[Task]:
        """Return task by id."""
        return self._tasks.get(task_id)

    def _dependencies_satisfied(self, task_id: str) -> bool:
        """True if all dependency tasks are merged."""
        task = self._tasks.get(task_id)
        if not task or not task.dependencies:
            return True
        for dep_id in task.dependencies:
            dep = self._tasks.get(dep_id)
            if not dep or dep.status != TaskStatus.MERGED:
                return False
        return True

    def assign_task_to_agent(self, task_id: str, agent_id: str) -> bool:
        """
        Assign task T to agent A. Allowed only if:
        - A has no current task or A's current task has status merged
        - T's dependencies are all merged
        - T exists and is in TO_DO or not yet assigned
        Returns True if assigned, False otherwise.
        """
        task = self._tasks.get(task_id)
        if not task:
            logger.warning("Task %s not found", task_id)
            return False
        current = self._agent_to_task.get(agent_id)
        if current:
            current_task = self._tasks.get(current)
            if current_task and current_task.status != TaskStatus.MERGED:
                logger.warning("Agent %s already has active task %s", agent_id, current)
                return False
            self._agent_to_task.pop(agent_id, None)
        if not self._dependencies_satisfied(task_id):
            logger.warning("Task %s dependencies not satisfied", task_id)
            return False
        task.status = TaskStatus.IN_PROGRESS
        task.assigned_agent_id = agent_id
        self._agent_to_task[agent_id] = task_id
        self._maybe_persist()
        return True

    def get_task_for_agent(self, agent_id: str) -> Optional[Task]:
        """Return the single task assigned to this agent that is not merged (in_progress or in_review)."""
        task_id = self._agent_to_task.get(agent_id)
        if not task_id:
            return None
        task = self._tasks.get(task_id)
        if not task or task.status == TaskStatus.MERGED:
            self._agent_to_task.pop(agent_id, None)
            return None
        return task

    def mark_branch_merged(self, task_id: str) -> bool:
        """Set task status to merged and merged_at = now; agent is then free for next assignment."""
        task = self._tasks.get(task_id)
        if not task:
            return False
        task.status = TaskStatus.MERGED
        task.merged_at = datetime.now(timezone.utc)
        if task.assigned_agent_id and self._agent_to_task.get(task.assigned_agent_id) == task_id:
            del self._agent_to_task[task.assigned_agent_id]
        self._maybe_persist()
        return True

    def set_task_in_review(self, task_id: str) -> bool:
        """Mark task as In Review (Senior SWE handed off feature branch)."""
        task = self._tasks.get(task_id)
        if not task:
            return False
        task.status = TaskStatus.IN_REVIEW
        self._maybe_persist()
        return True

    def get_next_eligible_subtask(self, task_id: str) -> Optional[Any]:
        """Return the next subtask that does not depend on an incomplete subtask, or None."""
        task = self._tasks.get(task_id)
        if not task or not task.subtasks:
            return None
        completed_ids = {s.id for s in task.subtasks if s.status == TaskStatus.MERGED}
        for st in task.subtasks:
            if st.status == TaskStatus.MERGED:
                continue
            if all(dep in completed_ids for dep in st.dependencies):
                return st
        return None

    def snapshot(self) -> Dict[str, Any]:
        """Return serializable snapshot for persistence."""
        from coding_team.models import TaskStatus as TS

        tasks_data = []
        for t in self._tasks.values():
            tasks_data.append({
                "id": t.id,
                "title": t.title,
                "description": t.description,
                "dependencies": t.dependencies,
                "status": t.status.value if isinstance(t.status, TS) else str(t.status),
                "assigned_agent_id": t.assigned_agent_id,
                "feature_branch": t.feature_branch,
                "merged_at": t.merged_at.isoformat() if t.merged_at else None,
                "acceptance_criteria": t.acceptance_criteria,
                "out_of_scope": t.out_of_scope,
                "priority": t.priority,
                "subtasks": [
                    {
                        "id": s.id,
                        "title": s.title,
                        "description": s.description,
                        "dependencies": s.dependencies,
                        "status": s.status.value if isinstance(s.status, TS) else str(s.status),
                    }
                    for s in t.subtasks
                ],
            })
        return {
            "job_id": self.job_id,
            "tasks": tasks_data,
            "agent_task_map": dict(self._agent_to_task),
        }

    def restore(self, snapshot: Dict[str, Any]) -> None:
        """Restore from a snapshot (e.g. from job store)."""
        from coding_team.models import Subtask

        self._tasks.clear()
        self._agent_to_task.clear()
        for tdata in snapshot.get("tasks", []):
            subtasks = []
            for s in tdata.get("subtasks", []):
                st = Subtask(
                    id=s["id"],
                    title=s.get("title", ""),
                    description=s.get("description", ""),
                    dependencies=s.get("dependencies", []),
                    status=TaskStatus(s.get("status", "to_do")),
                )
                subtasks.append(st)
            task = Task(
                id=tdata["id"],
                title=tdata.get("title", ""),
                description=tdata.get("description", ""),
                dependencies=tdata.get("dependencies", []),
                status=TaskStatus(tdata.get("status", "to_do")),
                assigned_agent_id=tdata.get("assigned_agent_id"),
                feature_branch=tdata.get("feature_branch"),
                merged_at=datetime.fromisoformat(tdata["merged_at"].replace("Z", "+00:00")) if tdata.get("merged_at") else None,
                acceptance_criteria=tdata.get("acceptance_criteria", []),
                out_of_scope=tdata.get("out_of_scope", ""),
                priority=tdata.get("priority", "medium"),
                subtasks=subtasks,
            )
            self._tasks[task.id] = task
        self._agent_to_task = dict(snapshot.get("agent_task_map", {}))

    def _maybe_persist(self) -> None:
        if self._persist:
            try:
                self._persist()
            except Exception as e:
                logger.warning("Task graph persist failed: %s", e)


def create_task_graph(
    job_id: str,
    persist_callback: Optional[Callable[[], None]] = None,
) -> TaskGraphService:
    """Create a new TaskGraphService for the given job."""
    return TaskGraphService(job_id=job_id, persist_callback=persist_callback)
