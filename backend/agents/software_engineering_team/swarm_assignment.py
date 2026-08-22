"""CodingTeamSwarm assignment mixin: ready-task discovery and Tech-Lead-driven assignment.

Extracted from ``coding_team/orchestrator.py`` (issue: decompose the orchestrator
god-file into named collaborators) — pure structural move, no behavior change.
Composed onto ``CodingTeamSwarm`` in orchestrator.py alongside the implementation
and review mixins.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from software_engineering_team.models import Task, TaskStatus
from software_engineering_team.team_routing import _target_matches_agent

logger = logging.getLogger(__name__)


class _AssignmentMixin:
    """Ready-task discovery and coordinator-driven assignment for CodingTeamSwarm."""

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

    def _pinned_agent_for(self, task: Task) -> Optional[str]:
        """The agent this task's feature branch is pinned to, or None if unpinned.

        A task with a recorded ``feature_branch_agent_id`` (set once, on first reaching
        review — see ``swarm_implementation._implement_and_verify``) must be reassigned to
        that SAME agent on any later round: the branch only exists checked out in that
        agent's isolated git worktree, and git refuses to check it out (or delete/recreate
        it) from any other worktree while it stays attached there — routing the task to a
        different agent would make branch preparation fail every round until the task
        exhausts its revisions.

        Postconditions:
            - Returns the pinned agent id when the task has one and that agent is still a
              member of this swarm's roster. Returns None when the task has never reached
              review (no branch exists yet) or the pinned agent has left the roster (e.g. a
              roster change across a retry) — a pin to an agent that no longer exists is not
              enforceable and is treated as unpinned.
        """
        agent_id = task.feature_branch_agent_id
        if agent_id and agent_id in self.agent_ids:
            return agent_id
        return None

    def _reserve_pinned_tasks(
        self, ready: List[Task], free_agents: List[str], used_agents: set[str]
    ) -> set[str]:
        """Claim each pinned task's agent before any Tech-Lead or guardrail assignment runs.

        Reservation must happen first, not merely be enforced as a rejection rule inside the
        later loops: if an unrelated proposal in the SAME Tech-Lead response claims a pinned
        task's only eligible agent before that pinned task's own (rejected, mismatched)
        proposal is processed, the guardrail pass would then find the agent already in
        ``used_agents`` and leave the pinned task idle for the round — a revision that must
        return to its branch owner could starve if that assignment pattern recurs.

        Preconditions:
            - ``used_agents`` is empty (called once, before any other assignment this round).
        Postconditions:
            - Every pinned task in ``ready`` whose pinned agent is free and target-matching is
              assigned to that agent. Returns the set of newly-assigned task ids; callers must
              skip these in their own passes.
        """
        assigned: set[str] = set()
        for task in ready:
            pinned = self._pinned_agent_for(task)
            if not pinned or pinned not in free_agents or pinned in used_agents:
                continue
            if not _target_matches_agent(
                task.target_team, self.agent_team_keys.get(pinned, pinned)
            ):
                continue
            if self._try_assign(task.id, pinned):
                used_agents.add(pinned)
                assigned.add(task.id)
        return assigned

    def _try_deterministic_assign(
        self,
        ready: List[Task],
        free_agents: List[str],
        used_agents: set[str],
        assigned_tasks: set[str],
    ) -> bool:
        """Assign every still-unassigned ready task via pure ``target_team`` matching,
        bypassing the Tech-Lead LLM call, when the mapping is unambiguous.

        Reuses ``_target_matches_agent`` (the same predicate the LLM-path guardrails use) to
        find each remaining task's candidate agents among the remaining free agents. The
        mapping is unambiguous only when every remaining task has exactly one candidate and no
        two tasks share the same candidate — this also covers the trivial "one free agent, one
        ready task" case, since an untargeted task matches any agent and a single remaining
        free agent is then its sole candidate.

        A pinned task whose pinned agent isn't free this round is excluded from this pool
        entirely rather than treated as a candidate for some other free agent: only
        ``_reserve_pinned_tasks`` (or the pinned-agent check in the LLM path) may place a
        pinned task, and only onto its own pinned agent — never here.

        Preconditions:
            - ``used_agents`` and ``assigned_tasks`` already reflect this round's pinned-task
              reservation (``_reserve_pinned_tasks``) and nothing else.
        Postconditions:
            - Returns False and makes no assignments if there is no remaining, unpinned ready
              task or free agent to place, if any such task has zero or more than one matching
              free agent, or if two such tasks would resolve to the same agent — callers must
              then fall through to the unchanged Tech-Lead LLM path.
            - Returns True only when the mapping is unambiguous; in that case every remaining,
              unpinned ready task is attempted via ``self._try_assign``, with ``used_agents``
              and ``assigned_tasks`` updated in place for each successful placement, and no LLM
              call is made.
        """
        remaining_ready = [
            t for t in ready if t.id not in assigned_tasks and not self._pinned_agent_for(t)
        ]
        remaining_free = [a for a in free_agents if a not in used_agents]
        if not remaining_ready or not remaining_free:
            return False

        agent_for_task: dict[str, str] = {}
        for task in remaining_ready:
            matches = [
                agent_id
                for agent_id in remaining_free
                if _target_matches_agent(
                    task.target_team, self.agent_team_keys.get(agent_id, agent_id)
                )
            ]
            if len(matches) != 1:
                return False
            agent_for_task[task.id] = matches[0]

        if len(set(agent_for_task.values())) != len(agent_for_task):
            return False

        for task in remaining_ready:
            agent_id = agent_for_task[task.id]
            if self._try_assign(task.id, agent_id):
                used_agents.add(agent_id)
                assigned_tasks.add(task.id)
        return True

    def _assign_tasks(self, ready: List[Task], free_agents: List[str]) -> None:
        """Coordinator decides which tasks go to which workers."""
        if not free_agents or not ready:
            return
        ready_by_id = {t.id: t for t in ready}
        used_agents: set[str] = set()
        assigned_tasks: set[str] = self._reserve_pinned_tasks(ready, free_agents, used_agents)
        if not self._try_deterministic_assign(ready, free_agents, used_agents, assigned_tasks):
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
                if (
                    not agent_id
                    or not task
                    or agent_id not in free_agents
                    or agent_id in used_agents
                ):
                    continue
                pinned = self._pinned_agent_for(task)
                if pinned and agent_id != pinned:
                    logger.warning(
                        "Ignoring assignment of task %s to agent %s; its feature branch is "
                        "pinned to %s",
                        task.id,
                        agent_id,
                        pinned,
                    )
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

        # Deterministic guardrail: if the Tech Lead already labeled a ready task for a v2 team
        # (or the task is pinned to a specific agent) but the assignment call omitted it, assign
        # it to a matching free worker rather than leaving it idle or routing it to the wrong
        # specialist.
        for task in ready:
            if task.id in assigned_tasks:
                continue
            pinned = self._pinned_agent_for(task)
            if not task.target_team and not pinned:
                continue
            # A pinned task has exactly one eligible candidate — its own agent, or nobody this
            # round if that agent isn't free yet. Never fall back to a different free worker,
            # which is exactly the reassignment this pin exists to prevent.
            candidates = [pinned] if pinned else free_agents
            for agent_id in candidates:
                if agent_id not in free_agents or agent_id in used_agents:
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
