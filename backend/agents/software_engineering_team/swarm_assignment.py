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
from software_engineering_team.team_routing import _target_matches_agent, _team_key

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
        cannot abort the whole assignment round. Returns True only on a real placement.

        Every assignment path in this mixin (pinned reservation, the deterministic fast
        paths, the Tech-Lead proposals, and the trailing guardrail loop) places tasks
        through here, which is why the least-recently-used bookkeeping that
        ``_try_homogeneous_target_assign`` ranks by is recorded here rather than in that
        one path: an agent occupied by a task is equally unavailable however it was
        placed, so a ranking that only counted fast-path placements would treat an agent
        that just finished other work as though it had been idle all along.

        Postconditions:
            - On a successful placement, ``self._agent_last_placed[agent_id]`` is set to
              the current ``self._placement_ordinal`` and that ordinal is incremented, so
              later placements always compare as more recent. A failed or raising
              placement leaves both untouched.
        """
        try:
            placed = bool(self.graph.assign_task_to_agent(task_id, agent_id))
        except Exception as exc:  # noqa: BLE001 - keep assigning the remaining ready tasks
            logger.warning("Failed to assign task %s to agent %s: %s", task_id, agent_id, exc)
            return False
        if placed:
            self._agent_last_placed[agent_id] = self._placement_ordinal
            self._placement_ordinal += 1
        return placed

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

    def _matching_free_agents(self, target_team: Optional[str], candidates: List[str]) -> List[str]:
        """Agents in ``candidates`` whose team can execute a task targeting ``target_team``.

        Preconditions:
            - ``candidates`` holds agent ids; ids absent from ``self.agent_team_keys`` are
              matched on the id itself (the same defaulting every other match site here
              uses), so an unknown id is never silently dropped.
        Postconditions:
            - Returns a new list preserving ``candidates`` order, filtered by
              ``_target_matches_agent``. A ``target_team`` that normalizes to an empty team
              key (``None``, ``""``, or a whitespace/punctuation-only label — see
              ``_team_key``) matches every agent, so the result is every candidate; callers
              that must treat such a task as untargeted have to test the normalized key
              themselves rather than infer it from this result.
            - Never mutates ``candidates`` or any swarm state.
        """
        return [
            agent_id
            for agent_id in candidates
            if _target_matches_agent(target_team, self.agent_team_keys.get(agent_id, agent_id))
        ]

    def _try_homogeneous_target_assign(
        self,
        remaining_ready: List[Task],
        remaining_free: List[str],
        used_agents: set[str],
        assigned_tasks: set[str],
    ) -> bool:
        """Fan a batch of tasks that all resolve to the identical free-agent pool out
        across that pool, bypassing the Tech-Lead LLM, when there's no contention for it.

        A roster with multiple workers per stack lets one ``target_team`` legitimately
        match 2+ free ``agent_id``s of the same stack — that is not real ambiguity
        requiring the Tech Lead's judgment, it's several interchangeable workers able to
        take several interchangeable tasks. Real ambiguity is a *choice* between different
        stacks (untargeted tasks) or genuine contention (more same-team tasks than matching
        free agents, which needs prioritization). "Same team" is judged by each task's
        *resolved candidate set* (via ``_matching_free_agents``), not by comparing raw
        ``target_team`` strings: two tasks spelled e.g. ``"backend"`` and ``"backend_v2"``
        both resolve to the same ``backend_v2`` agents via ``_target_matches_agent``'s own
        alias normalization and must be recognized as one homogeneous batch, exactly like
        every other match in this file already does. This only ever fires for the narrow,
        unambiguous "N tasks that all resolve to the identical M-agent pool, M>=N" shape;
        every other shape (mixed pools, untargeted tasks, or contention) is left to the
        caller's existing strict one-candidate-per-task logic.

        A pool larger than its task batch (e.g. one ready task, two free same-stack
        agents) picks the least-recently-used matching agent(s) first, ranked by
        ``self._agent_last_placed`` (agent_id -> the ``self._placement_ordinal`` value at
        its last placement through ``_try_assign``, which every assignment path in this
        mixin goes through; an agent with no entry ranks oldest, ahead of any placed
        agent). Ranking by per-agent placement history — not a positional offset into
        whichever candidate list happens to be live this round — is what keeps this
        correct as the free/busy set churns: an index/offset rotation desyncs the moment
        the candidate list's *membership* (not just its length) changes between calls,
        which can silently starve an agent indefinitely even though it's free every round
        it's a candidate. Only successful placements update the ranking, so a transient
        ``_try_assign`` failure for one task never costs its agent its earned priority.

        Preconditions:
            - ``remaining_ready`` and ``remaining_free`` are the caller's already-filtered
              pools (unpinned/unassigned ready tasks; free agents not yet used this round)
              — the same precondition ``_try_deterministic_assign`` documents for its own
              inputs, inherited unchanged since this is only ever called from there.
            - ``remaining_ready`` is non-empty (the caller returns early otherwise). The
              size guard below relies on this to reject an empty matching-agent pool, and
              the first task is read directly to seed the batch's team.
        Postconditions:
            - Returns False and makes no assignments unless every task in
              ``remaining_ready`` has a non-empty ``target_team`` and they all resolve to
              the exact same non-empty set of matching free agents, and that set has at
              least as many agents as there are tasks. The ``target_team`` requirement
              covers every task, not merely the first: an untargeted task matches every
              agent, so on an all-one-stack free pool it would otherwise resolve to the
              same set as its same-stack batch-mates and be absorbed into them.
            - Returns True otherwise: every task in ``remaining_ready`` is attempted via
              ``self._try_assign`` against a least-recently-used-first ordering of the
              matching agents, with ``used_agents``/``assigned_tasks`` updated in place for
              each successful placement (``_try_assign`` records the LRU bookkeeping
              itself). A pool larger than the task batch simply leaves its lowest-priority
              excess agents unused this round — the size guard above guarantees there is
              never a task this method fails to attempt.
        """
        # Every task, not just the first: an untargeted task matches *every* agent, so
        # when the free pool is all one stack it resolves to the same set a same-stack
        # targeted task does and would otherwise be silently absorbed into the batch.
        # Test the normalized key, not raw truthiness — _team_key collapses a
        # whitespace/punctuation-only label to "", which matches every agent exactly the
        # way None does, so such a task is untargeted despite being truthy in Python.
        if not all(_team_key(task.target_team) for task in remaining_ready):
            return False
        first_team = remaining_ready[0].target_team
        matching_free = self._matching_free_agents(first_team, remaining_free)
        # Covers an empty pool too: remaining_ready is non-empty by precondition, so a
        # zero-length matching_free always trips this.
        if len(matching_free) < len(remaining_ready):
            return False

        matching_set = set(matching_free)
        for task in remaining_ready[1:]:
            if set(self._matching_free_agents(task.target_team, remaining_free)) != matching_set:
                return False

        last_placed = self._agent_last_placed
        ordered_free = sorted(matching_free, key=lambda aid: last_placed.get(aid, -1))

        for task, agent_id in zip(remaining_ready, ordered_free):
            if self._try_assign(task.id, agent_id):
                used_agents.add(agent_id)
                assigned_tasks.add(task.id)
        return True

    def _try_deterministic_assign(
        self,
        ready: List[Task],
        free_agents: List[str],
        used_agents: set[str],
        assigned_tasks: set[str],
    ) -> bool:
        """Assign every still-unassigned ready task via pure ``target_team`` matching,
        bypassing the Tech-Lead LLM call, when the mapping is unambiguous.

        Tries ``_try_homogeneous_target_assign`` first (same-team tasks, no contention for
        the matching same-stack agents), then falls back to matching each remaining task
        to its candidate agents among the remaining free agents via
        ``_target_matches_agent`` (the same predicate the LLM-path guardrails use). That
        fallback mapping is unambiguous only when every remaining task has exactly one
        candidate and no two tasks share the same candidate — this also covers the trivial
        "one free agent, one ready task" case, since an untargeted task matches any agent
        and a single remaining free agent is then its sole candidate.

        A pinned task whose pinned agent isn't free this round is excluded from this pool
        entirely rather than treated as a candidate for some other free agent: only
        ``_reserve_pinned_tasks`` (or the pinned-agent check in the LLM path) may place a
        pinned task, and only onto its own pinned agent — never here.

        Preconditions:
            - ``used_agents`` and ``assigned_tasks`` already reflect this round's pinned-task
              reservation (``_reserve_pinned_tasks``) and nothing else.
        Postconditions:
            - Returns False and makes no assignments when neither path applies: there is no
              remaining unpinned ready task or free agent to place; the batch is not
              homogeneous (see ``_try_homogeneous_target_assign`` for that path's own
              conditions); *and*, in the per-task fallback, some task has zero or more than
              one matching free agent, or two tasks would resolve to the same agent —
              callers must then fall through to the unchanged Tech-Lead LLM path. Note that
              "more than one matching free agent" is disqualifying only in that fallback;
              it is precisely the case the homogeneous path exists to place.
            - Returns True when the mapping is unambiguous (directly, or via the homogeneous
              fast path); in that case every task in ``remaining_ready`` is attempted via
              ``self._try_assign`` (regardless of whether the attempt succeeds), with
              ``used_agents`` and ``assigned_tasks`` updated in place for each successful
              placement, and no LLM call is made.
        """
        remaining_ready = [
            t for t in ready if t.id not in assigned_tasks and not self._pinned_agent_for(t)
        ]
        remaining_free = [a for a in free_agents if a not in used_agents]
        if not remaining_ready or not remaining_free:
            return False

        if self._try_homogeneous_target_assign(
            remaining_ready, remaining_free, used_agents, assigned_tasks
        ):
            return True

        agent_for_task: dict[str, str] = {}
        for task in remaining_ready:
            matches = self._matching_free_agents(task.target_team, remaining_free)
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
