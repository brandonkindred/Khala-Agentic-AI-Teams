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

# Sentinel distinguishing "argument not supplied" from an explicit None in update_task, so a caller
# can clear an assignment with assigned_agent_id=None without it being indistinguishable from the
# default. None previously meant "leave untouched", which silently dropped clear requests.
_UNSET: Any = object()


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
        # Bumped on every mutation (and restore). Lets a persister detect a no-op
        # call and skip a redundant snapshot+write — the swarm loop persists several
        # times per round, often with no change in between.
        self.revision = 0

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

    def _free_agent(self, task: Task) -> None:
        """Release the agent currently mapped to *task*, if it still points back at this task.

        A task in a terminal state (MERGED or FAILED) holds no agent — the worker is free to pick
        up the next assignment. This is the single place that enforces that invariant; every
        terminal transition (`update_task(status=FAILED)`, `mark_branch_merged`,
        `mark_dependents_failed`) routes through here so a new terminal path cannot forget it.

        Preconditions:
            - `task` is a task tracked by this graph.
        Postconditions:
            - `self._agent_to_task` contains no entry pointing at `task.id`.
        """
        aid = task.assigned_agent_id
        if aid and self._agent_to_task.get(aid) == task.id:
            del self._agent_to_task[aid]

    def update_task(
        self,
        task_id: str,
        status: Optional[TaskStatus] = None,
        assigned_agent_id: Any = _UNSET,
        feature_branch: Optional[str] = None,
        merged_at: Optional[datetime] = None,
        **kwargs: Any,
    ) -> Optional[Task]:
        """Update task fields. Returns the task if found.

        Passing `assigned_agent_id=None` explicitly clears the assignment AND releases the agent
        (equivalent to `unassign_task`); omitting it leaves the assignment untouched. Clearing must
        free the agent->task mapping too — nulling only the back-reference would leave the agent
        marked busy, which is the silent-no-op bug this sentinel design removes.
        """
        task = self._tasks.get(task_id)
        if not task:
            return None
        if status is not None:
            task.status = status
            # A task transitioning to FAILED frees its agent immediately — symmetric with
            # `mark_branch_merged`/`mark_dependents_failed`. Without this the mapping lingers
            # until the next `get_task_for_agent` lazily prunes it, so a terminal snapshot
            # persisted right after the failure would still report the agent as occupied.
            if status == TaskStatus.FAILED:
                self._free_agent(task)
        if assigned_agent_id is not _UNSET:
            if assigned_agent_id is None:
                self._free_agent(task)  # uses task.assigned_agent_id — must run before we null it
                task.assigned_agent_id = None
            else:
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

    def unassign_task(self, task_id: str) -> None:
        """Clear a task's agent assignment and release the agent that held it.

        Thin wrapper over `update_task(task_id, assigned_agent_id=None)` for callers whose intent is
        purely to release a task (e.g. a quality-gate rejection demoting it to TO_DO): the task must
        become genuinely unassigned so a free agent can re-pick it and the old agent is freed —
        otherwise the task is TO_DO yet still mapped to its agent, and the next round can both
        re-serve it to that agent and assign it to a second free agent (two workers, one task).

        Preconditions:
            - `task_id` refers to a task tracked by this graph (no-op if it does not).
        Postconditions:
            - `task.assigned_agent_id is None` and no agent in `_agent_to_task` maps to `task_id`.
        """
        self.update_task(task_id, assigned_agent_id=None)

    def reset_in_flight(self) -> None:
        """Demote every non-terminal in-flight task to TO_DO and release its agent.

        Used on resume from a persisted snapshot (e.g. a Temporal retry of the same job): a task
        left IN_PROGRESS or IN_REVIEW when the previous attempt died may hold only partial work, and
        its agent mapping refers to workers that no longer exist in this run. Demoting these to
        unassigned TO_DO lets the fresh swarm re-plan and re-pick them deterministically. MERGED and
        FAILED tasks (terminal) are left untouched. Agent release routes through `_free_agent` (the
        single place that maintains the agent->task mapping invariant) rather than clearing the map
        directly, so the two stay consistent if that mapping ever gains structure.

        Postconditions:
            - No task is IN_PROGRESS or IN_REVIEW; no in-flight task retains an agent mapping.
        """
        for task in self._tasks.values():
            if task.status in (TaskStatus.IN_PROGRESS, TaskStatus.IN_REVIEW):
                task.status = TaskStatus.TO_DO
                self._free_agent(task)  # uses task.assigned_agent_id — must run before we null it
                task.assigned_agent_id = None
        self._maybe_persist()

    def get_tasks(self) -> List[Task]:
        """Return all tasks (copy)."""
        return list(self._tasks.values())

    def get_task(self, task_id: str) -> Optional[Task]:
        """Return task by id."""
        return self._tasks.get(task_id)

    def count_with_status(self, status: TaskStatus) -> int:
        """Number of tasks currently in *status*. Single source of truth for status tallies."""
        return sum(1 for t in self._tasks.values() if t.status == status)

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
        if not task or task.status in (TaskStatus.MERGED, TaskStatus.FAILED):
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
        self._free_agent(task)
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

    def mark_dependents_failed(self, task_id: str) -> List[str]:
        """Cascade-FAIL every task that (transitively) depends on a FAILED task.

        A FAILED dependency can never become MERGED, so any task that depends on it can never
        satisfy `_dependencies_satisfied` and would otherwise keep the swarm loop from ever
        completing (it stays TO_DO, `_is_complete()` stays false, the loop spins to max_rounds).
        This marks all such tasks FAILED to a fixpoint, frees any agent mapped to them, and
        records why.

        Preconditions:
            - `task_id` refers to a task already in FAILED status.
        Postconditions:
            - No non-FAILED task has a FAILED task among its dependencies.
        Returns the ids newly marked FAILED (excludes `task_id` itself).
        """
        newly_failed: List[str] = []
        changed = True
        while changed:
            changed = False
            for t in self._tasks.values():
                if t.status == TaskStatus.FAILED or not t.dependencies:
                    continue
                if any(
                    (dep := self._tasks.get(d)) is not None and dep.status == TaskStatus.FAILED
                    for d in t.dependencies
                ):
                    t.status = TaskStatus.FAILED
                    t.revision_feedback = list(t.revision_feedback or []) + [
                        {"source": "system", "reason": "Blocked: a required dependency failed"}
                    ]
                    self._free_agent(t)
                    newly_failed.append(t.id)
                    changed = True
        if newly_failed:
            self._maybe_persist()
        return newly_failed

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
            tasks_data.append(
                {
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
                    "changes_summary": t.changes_summary,
                    "revision_count": t.revision_count,
                    "revision_feedback": t.revision_feedback,
                    "no_change_revisits": t.no_change_revisits,
                    "last_change_digest": t.last_change_digest,
                    "resolved_without_changes": t.resolved_without_changes,
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
                }
            )
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
                merged_at=datetime.fromisoformat(tdata["merged_at"].replace("Z", "+00:00"))
                if tdata.get("merged_at")
                else None,
                acceptance_criteria=tdata.get("acceptance_criteria", []),
                out_of_scope=tdata.get("out_of_scope", ""),
                priority=tdata.get("priority", "medium"),
                subtasks=subtasks,
                changes_summary=tdata.get("changes_summary"),
                revision_count=tdata.get("revision_count", 0),
                revision_feedback=tdata.get("revision_feedback", []),
                no_change_revisits=tdata.get("no_change_revisits", 0),
                last_change_digest=tdata.get("last_change_digest", ""),
                resolved_without_changes=tdata.get("resolved_without_changes", False),
            )
            self._tasks[task.id] = task
        self._agent_to_task = dict(snapshot.get("agent_task_map", {}))
        # A wholesale state replacement — bump so a subsequent persist isn't
        # mistaken for a no-op and skipped.
        self.revision += 1

    def _maybe_persist(self) -> None:
        self.revision += 1
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
