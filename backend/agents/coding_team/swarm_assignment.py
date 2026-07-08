"""CodingTeamSwarm assignment mixin: ready-task discovery and Tech-Lead-driven assignment.

Extracted from ``coding_team/orchestrator.py`` (issue: decompose the orchestrator
god-file into named collaborators) — pure structural move, no behavior change.
Composed onto ``CodingTeamSwarm`` in orchestrator.py alongside the implementation
and review mixins.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from coding_team.models import Task, TaskStatus
from coding_team.team_routing import _target_matches_agent

logger = logging.getLogger(__name__)


class _AssignmentMixin:
    """Ready-task discovery and coordinator-driven assignment for CodingTeamSwarm."""

    def _merged_count(self) -> int:
        return self.graph.count_with_status(TaskStatus.MERGED)

    def _find_ready_tasks(self) -> List[Task]:
        return [
            t
            for t in self.graph.get_tasks()
            if t.status == TaskStatus.TO_DO and self.graph._dependencies_satisfied(t.id)
        ]

    def _find_free_agents(self) -> List[str]:
        return [aid for aid in self.agent_ids if self.graph.get_task_for_agent(aid) is None]

    def _has_worker_for_target(self, target_team: Optional[str]) -> bool:
        """Whether any worker in this swarm can ever satisfy the target team hint."""
        if not target_team:
            return True
        return any(
            _target_matches_agent(target_team, self.agent_team_keys.get(agent_id, agent_id))
            for agent_id in self.agent_ids
        )

    def _try_assign(self, task_id: str, agent_id: str) -> bool:
        """Assign a task to an agent, swallowing transient errors so one bad assignment
        cannot abort the whole assignment round. Returns True only on a real placement."""
        try:
            return bool(self.graph.assign_task_to_agent(task_id, agent_id))
        except Exception as exc:  # noqa: BLE001 - keep assigning the remaining ready tasks
            logger.warning("Failed to assign task %s to agent %s: %s", task_id, agent_id, exc)
            return False

    def _assign_tasks(self, ready: List[Task], free_agents: List[str]) -> None:
        """Coordinator decides which tasks go to which workers."""
        if not free_agents or not ready:
            return
        ready_by_id = {t.id: t for t in ready}
        used_agents: set[str] = set()
        assigned_tasks: set[str] = set()
        assignments = self.tech_lead.run_assignments(
            agent_ids=self.agent_ids,
            ready_tasks=[
                {
                    "id": t.id,
                    "title": t.title,
                    "target_team": t.target_team or "",
                    "assignee": t.assigned_agent_id or "unassigned",
                }
                for t in ready
            ],
            free_agents=free_agents,
        )
        for a in assignments.get("assignments") or []:
            agent_id = a.get("agent_id")
            task_id = a.get("task_id")
            task = ready_by_id.get(task_id)
            if not agent_id or not task or agent_id not in free_agents or agent_id in used_agents:
                continue
            if not _target_matches_agent(
                task.target_team,
                self.agent_team_keys.get(agent_id, agent_id),
            ):
                logger.warning(
                    "Ignoring assignment of task %s target_team=%s to mismatched agent %s",
                    task.id,
                    task.target_team,
                    agent_id,
                )
                continue
            if self._try_assign(task.id, agent_id):
                used_agents.add(agent_id)
                assigned_tasks.add(task.id)

        # Deterministic guardrail: if the Tech Lead already labeled a ready task for a v2 team but
        # the assignment call omitted it, assign it to a matching free worker rather than leaving it
        # idle or routing it to the wrong specialist.
        for task in ready:
            if not task.target_team or task.id in assigned_tasks:
                continue
            for agent_id in free_agents:
                if agent_id in used_agents:
                    continue
                if not _target_matches_agent(
                    task.target_team, self.agent_team_keys.get(agent_id, agent_id)
                ):
                    continue
                # Only stop once the task is actually placed. If assignment fails (e.g. the
                # agent's prior task isn't merged yet, or a transient error), keep trying the
                # remaining matching free workers instead of leaving the task idle for the round.
                if self._try_assign(task.id, agent_id):
                    used_agents.add(agent_id)
                    assigned_tasks.add(task.id)
                    break

        for task in ready:
            if task.id in assigned_tasks or not task.target_team:
                continue
            if self._has_worker_for_target(task.target_team):
                continue
            reason = f"No implementation worker is available for target_team {task.target_team!r}."
            logger.warning("Failing task %s: %s", task.id, reason)
            self.graph.update_task(
                task.id,
                status=TaskStatus.FAILED,
                changes_summary=reason,
                revision_feedback=list(task.revision_feedback or [])
                + [{"source": "system", "reason": reason}],
            )
            self._cascade_fail_dependents(task.id)
