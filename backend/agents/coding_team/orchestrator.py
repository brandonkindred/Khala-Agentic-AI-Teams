"""
Coding team orchestrator: plan → Task Graph → assign → implement → review → merge.

Uses a swarm pattern: a Coordinator (Tech Lead) assigns tasks from the graph
to Workers (Senior SWEs). Quality gate tools run after each implementation.
Exposes run_coding_team_orchestrator for in-process call from software_engineering_team.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from coding_team.job_store import (
    DEFAULT_CACHE_DIR,
    get_job,
    update_job,
    update_job_task_graph,
)
from coding_team.models import (
    CodingTeamPlanInput,
    StackSpec,
    Task,
    TaskStatus,
)
from coding_team.senior_software_engineer_agent import SeniorSWEAgent
from coding_team.task_graph import TaskGraphService, create_task_graph
from coding_team.tech_lead_agent import TechLeadAgent

logger = logging.getLogger(__name__)

CANCEL_KEY = "cancel_requested"
MAX_TASK_REVISIONS = 20  # max times a task can be returned for revision before accepting


def _read_repo_context(repo_path: Path, max_chars: int = 4000) -> str:
    """Read a short summary of repo structure/code for Senior SWE context."""
    parts: List[str] = []
    total = 0
    try:
        for f in sorted(repo_path.rglob("*"))[:80]:
            if not f.is_file() or f.suffix not in {
                ".py",
                ".ts",
                ".js",
                ".java",
                ".html",
                ".json",
                ".yaml",
                ".yml",
                # Markdown/plain-text docs (specs, plans, READMEs) must be visible too, or a worker
                # tasked with documentation reports the repo "empty" and recreates docs every round.
                ".md",
                ".txt",
                ".rst",
            }:
                continue
            if any(
                skip in f.parts for skip in ("node_modules", ".git", "__pycache__", "venv", ".venv")
            ):
                continue
            try:
                content = f.read_text(encoding="utf-8", errors="replace")[:500]
            except Exception:
                continue
            rel = str(f.relative_to(repo_path))
            chunk = f"--- {rel} ---\n{content}\n"
            if total + len(chunk) > max_chars:
                break
            parts.append(chunk)
            total += len(chunk)
    except Exception:
        pass
    return "\n".join(parts) if parts else "No files found"


def run_coding_team_orchestrator(
    job_id: str,
    repo_path: str | Path,
    plan_input: CodingTeamPlanInput,
    *,
    update_job_fn: Optional[Callable[..., None]] = None,
    get_job_fn: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
    get_llm: Optional[Callable[[str], Any]] = None,
) -> None:
    """
    Run the coding_team pipeline: plan → Task Graph → groom/assign → implement → review → merge.
    Uses in-process job store (coding_team/job_store) for task graph persistence.
    update_job_fn / get_job_fn: if provided (e.g. from software_engineering_team), used for phase/status and cancel check.
    """
    path = Path(repo_path).resolve()
    _update = update_job_fn or (lambda **kw: update_job(job_id, cache_dir=cache_dir, **kw))
    _get_job = get_job_fn or (lambda jid: get_job(jid, cache_dir=cache_dir))
    llm_getter = get_llm or (
        lambda key: __import__("llm_service.strands_provider", fromlist=["get_strands_model"]).get_strands_model(
            key or "coding_team"
        )
    )

    def _check_cancel() -> bool:
        data = _get_job(job_id)
        return bool(data and data.get(CANCEL_KEY))

    # Create Task Graph with persist
    def _persist_graph() -> None:
        snap = graph.snapshot()
        update_job_task_graph(job_id, snap, cache_dir=cache_dir)
        _update(phase=phase, status_text=status_text)

    graph: TaskGraphService = create_task_graph(job_id, persist_callback=_persist_graph)
    phase = "task_graph"
    status_text = "Building task graph from plan"

    # The Tech Lead object is needed for the swarm coordinator (assignments/reviews) regardless of
    # whether we plan fresh or resume, so build it unconditionally.
    llm = llm_getter("tech_lead")
    tech_lead = TechLeadAgent(llm)

    # Resume from a persisted snapshot (e.g. a Temporal retry of the same job_id) instead of
    # re-running the planning LLM and re-doing finished work. `_persist_graph` writes the task
    # snapshot every round; the stacks are persisted alongside it on the fresh path below.
    existing = _get_job(job_id) or {}
    snapshot_tasks = existing.get("task_graph_snapshot") or []
    if snapshot_tasks:
        logger.info("Resuming job %s from snapshot (%d tasks)", job_id, len(snapshot_tasks))
        graph.restore(
            {
                "tasks": snapshot_tasks,
                "agent_task_map": existing.get("agent_task_map") or {},
            }
        )
        # In-flight tasks from the dead attempt may be half-done and their agent mapping is stale,
        # so demote them to unassigned TO_DO; MERGED/FAILED are preserved (no re-work).
        graph.reset_in_flight()
        stacks_raw = existing.get("stack_specs") or [{"name": "default", "tools_services": []}]
    else:
        out = tech_lead.run_plan_to_task_graph(plan_input)
        tasks_raw = out.get("tasks") or []
        stacks_raw = out.get("stacks") or [{"name": "default", "tools_services": []}]
        for t in tasks_raw:
            graph.add_task(
                task_id=t["id"],
                title=t.get("title", t["id"]),
                description=t.get("description", ""),
                dependencies=t.get("dependencies", []),
            )
        # Persist the stacks so a later retry can rebuild the workers without re-planning.
        _update(stack_specs=stacks_raw)
    _persist_graph()

    # Build Senior SWE agents (one per stack)
    stack_specs: List[StackSpec] = []
    for i, s in enumerate(stacks_raw):
        name = s.get("name") or f"stack_{i}"
        tools = s.get("tools_services") or []
        stack_specs.append(StackSpec(name=name, tools_services=tools))
    agent_ids = [s.name or f"agent_{i}" for i, s in enumerate(stack_specs)]
    senior_swes: List[SeniorSWEAgent] = []
    for i, spec in enumerate(stack_specs):
        aid = agent_ids[i]
        llm_swe = llm_getter("coding_team")
        senior_swes.append(SeniorSWEAgent(agent_id=aid, stack_spec=spec, llm=llm_swe))

    phase = "coding"
    status_text = "Assigning and implementing tasks"
    _update(phase=phase, status_text=status_text, status="running")

    # Run the swarm: coordinator (Tech Lead) + workers (Senior SWEs)
    swarm = CodingTeamSwarm(
        tech_lead=tech_lead,
        workers=senior_swes,
        graph=graph,
        path=path,
        agent_ids=agent_ids,
        llm_getter=llm_getter,
    )
    swarm.run(
        check_cancel=_check_cancel,
        persist_fn=_persist_graph,
        update_fn=_update,
    )

    merged_count = sum(1 for t in graph.get_tasks() if t.status == TaskStatus.MERGED)
    failed_count = sum(1 for t in graph.get_tasks() if t.status == TaskStatus.FAILED)
    # A job with failed tasks must not be presented as a clean success — surface a distinct
    # terminal status so downstream consumers (and the GitHub publish flow) can flag the gap.
    _update(
        status="completed_with_failures" if failed_count else "completed",
        phase="completed",
        status_text=f"Completed: {merged_count} merged, {failed_count} failed",
    )


class CodingTeamSwarm:
    """Coordinator (Tech Lead) + Workers (Senior SWEs) swarm pattern.

    The coordinator assigns ready tasks to free workers. Each worker implements
    the task, runs quality gates (build, lint, code review), and signals
    completion. The coordinator reviews and merges approved tasks.
    """

    def __init__(
        self,
        tech_lead: TechLeadAgent,
        workers: List[SeniorSWEAgent],
        graph: TaskGraphService,
        path: Path,
        agent_ids: List[str],
        llm_getter: Callable[[str], Any],
    ) -> None:
        self.tech_lead = tech_lead
        self.workers = workers
        self.graph = graph
        self.path = path
        self.agent_ids = agent_ids
        self.llm_getter = llm_getter
        self.repo_context = _read_repo_context(path)

    def _find_ready_tasks(self) -> List[Task]:
        return [
            t for t in self.graph.get_tasks()
            if t.status == TaskStatus.TO_DO and self.graph._dependencies_satisfied(t.id)
        ]

    def _find_free_agents(self) -> List[str]:
        return [aid for aid in self.agent_ids if self.graph.get_task_for_agent(aid) is None]

    def _assign_tasks(self, ready: List[Task], free_agents: List[str]) -> None:
        """Coordinator decides which tasks go to which workers."""
        if not free_agents or not ready:
            return
        assignments = self.tech_lead.run_assignments(
            agent_ids=self.agent_ids,
            ready_tasks=[
                {"id": t.id, "title": t.title, "assignee": t.assigned_agent_id or "unassigned"}
                for t in ready
            ],
            free_agents=free_agents,
        )
        for a in assignments.get("assignments") or []:
            agent_id = a.get("agent_id")
            task_id = a.get("task_id")
            if agent_id and task_id:
                self.graph.assign_task_to_agent(task_id, agent_id)

    def _implement_and_verify(self, swe: SeniorSWEAgent, update_fn: Callable) -> None:
        """Worker implements its assigned task, then runs quality gate tools."""
        task = self.graph.get_task_for_agent(swe.agent_id)
        if not task:
            return
        # Only (re)implement a task that is actively assigned for work. An IN_REVIEW task is awaiting
        # Tech Lead review — re-running the engineer on it would regenerate code already under review
        # and churn the loop. With un-assignment fixed this is belt-and-suspenders, but it makes the
        # worker's contract explicit and robust against any upstream assignment slip.
        if task.status != TaskStatus.IN_PROGRESS:
            return

        update_fn(status_text=f"Implementing: {task.title}")
        result = swe.run_implement(task, self.path, repo_context=self.repo_context)

        if result.get("status") == "in_review":
            # Run quality gates as tools
            if not self._run_quality_gates(swe, task, result, update_fn):
                return  # task returned to TODO for revision
            self.graph.update_task(
                task.id,
                feature_branch=result.get("feature_branch"),
                changes_summary=result.get("changes_summary"),
            )
            self.graph.set_task_in_review(task.id)
        elif result.get("status") == "failed":
            logger.warning("Worker %s task %s failed: %s", swe.agent_id, task.id, result.get("error"))
            self._handle_implement_failure(task, result)

    def _handle_implement_failure(self, task: Task, result: Dict[str, Any]) -> None:
        """Bound a failing implementation so it cannot spin the loop to max_rounds.

        A `run_implement` that returns status="failed" (e.g. the LLM call raised) was previously
        only logged, leaving the task IN_PROGRESS and assigned — so the same failing call repeated
        every round until the round cap. Count each failure against the shared revision cap and,
        on exhaustion, fail the task (and its dependents) terminally with the error recorded.
        """
        entry = {
            "source": "engineer",
            "reason": f"Implementation failed: {result.get('error') or 'unknown error'}",
            "requested_changes": [],
        }
        feedback = list(task.revision_feedback or []) + [entry]
        revision_count = task.revision_count + 1
        if revision_count >= MAX_TASK_REVISIONS:
            logger.warning(
                "Task %s implementation failed and exhausted revisions (%d); marking FAILED",
                task.id, MAX_TASK_REVISIONS,
            )
            self.graph.update_task(
                task.id,
                status=TaskStatus.FAILED,
                revision_count=revision_count,
                revision_feedback=feedback,
            )
            self._cascade_fail_dependents(task.id)
            return
        # Keep it with the same engineer for another bounded attempt; record the failure.
        self.graph.update_task(
            task.id,
            status=TaskStatus.IN_PROGRESS,
            revision_count=revision_count,
            revision_feedback=feedback,
        )

    def _run_quality_gates(
        self, swe: SeniorSWEAgent, task: Task, result: Dict[str, Any], update_fn: Callable
    ) -> bool:
        """Run build, lint, code review. Returns True if passed, False if returned for revision."""
        try:
            from software_engineering_team.quality_gate_tools import (
                run_build_verification,
                run_code_review,
                run_linting,
            )

            agent_type = swe.stack_spec.name or "backend"

            # Build verification
            update_fn(status_text=f"Build verification: {task.title}")
            build = run_build_verification(self.path, agent_type, task.id)
            if not build.success:
                logger.warning("[%s] Build failed for task %s: %s", swe.agent_id, task.id, build.error[:200])
                return self._return_for_revision(task, [{"type": "build", "error": build.error}])

            # Linting
            update_fn(status_text=f"Linting: {task.title}")
            run_linting(self.path, task.id, llm_getter=self.llm_getter)

            # Code review
            update_fn(status_text=f"Code review: {task.title}")
            review = run_code_review(
                code=result.get("changes_summary", ""),
                spec_content="",
                task_description=task.description or task.title,
                language="python" if agent_type == "backend" else "typescript",
                acceptance_criteria=task.acceptance_criteria or [],
                llm_getter=self.llm_getter,
            )
            if not review.approved:
                logger.info(
                    "[%s] Code review rejected task %s (%d issues); returning for revision",
                    swe.agent_id, task.id, len(review.issues),
                )
                return self._return_for_revision(task, review.issues)

        except ImportError:
            logger.debug("Quality gate tools not available; skipping")
        except Exception as e:
            logger.warning("Quality gate tools error for task %s: %s; proceeding", task.id, e)

        return True

    def _return_for_revision(self, task: Task, feedback: List[Dict[str, Any]]) -> bool:
        """Return a task to TODO for revision. Returns False (task not ready for review)."""
        revision_count = task.revision_count + 1
        if revision_count >= MAX_TASK_REVISIONS:
            logger.warning(
                "Task %s exceeded max revisions (%d); accepting as-is", task.id, MAX_TASK_REVISIONS
            )
            return True  # accept despite issues
        # Append to the accumulated history rather than overwriting it: a task may have prior
        # Tech Lead feedback that must survive a later quality-gate failure, or the next engineer
        # prompt would lose those requirements and the reviewer could reintroduce them.
        self.graph.update_task(
            task.id,
            status=TaskStatus.TO_DO,
            revision_count=revision_count,
            revision_feedback=list(task.revision_feedback or []) + list(feedback),
        )
        # `update_task` ignores assigned_agent_id=None by design, so clear the assignment explicitly:
        # the task must be genuinely unassigned (and the agent freed) before the next round, or it
        # stays mapped to its agent and can be double-assigned.
        self.graph.unassign_task(task.id)
        return False

    def _review_and_merge(self, update_fn: Callable) -> None:
        """Coordinator reviews completed tasks: merge approved ones, send rejected ones back."""
        from software_engineering_team.shared.git_utils import (
            DEVELOPMENT_BRANCH,
            branch_diff,
            merge_branch,
        )

        in_review = [t for t in self.graph.get_tasks() if t.status == TaskStatus.IN_REVIEW]
        for task in in_review:
            update_fn(status_text=f"Tech Lead reviewing: {task.title}")
            branch = task.feature_branch or f"feature/{task.id}"
            summary = task.changes_summary or "(no summary recorded)"
            diff = branch_diff(self.path, DEVELOPMENT_BRANCH, branch)
            evidence = summary + (f"\n\n--- DIFF ---\n{diff}" if diff else "")
            review = self.tech_lead.run_code_review(
                task_title=task.title,
                task_description=task.description,
                acceptance_criteria=task.acceptance_criteria,
                changes_summary=evidence,
            )
            if review.get("error"):
                # The review itself could not run (e.g. evidence exceeded the model context
                # window). Do NOT route this through the revision loop — re-sending the same
                # failing prompt every round would burn the whole revision budget at max cost.
                # Fail the task once with the diagnostic instead.
                self._fail_task(task, review, "Tech Lead review could not be completed")
            elif review.get("approved"):
                try:
                    ok, _ = merge_branch(self.path, branch, DEVELOPMENT_BRANCH)
                    if ok:
                        self.graph.mark_branch_merged(task.id)
                except Exception as e:
                    logger.warning("Merge failed for %s: %s; marking merged anyway", task.id, e)
                    self.graph.mark_branch_merged(task.id)
            else:
                self._request_revision(task, review)

    def _request_revision(self, task: Task, review: Dict[str, Any]) -> None:
        """Send a Tech-Lead-rejected task back to the SAME engineer for revision.

        Unlike the quality-gate path (_return_for_revision, which demotes to TO_DO and clears the
        assignment), a Tech Lead rejection keeps the task with its current engineer: status goes
        back to IN_PROGRESS so the same SWE re-runs run_implement next round with the reviewer's
        reasons threaded into the prompt. On exhausting MAX_TASK_REVISIONS the task is marked
        FAILED (terminal) rather than merging code the Tech Lead rejected.

        Preconditions:
            - task is currently IN_REVIEW and assigned to an engineer.
        Postconditions:
            - task.status is IN_PROGRESS (revision pending) or FAILED (exhausted); never left
              IN_REVIEW with no state change, so the swarm loop cannot deadlock on it.
        """
        entry = {
            "source": "tech_lead",
            "reason": review.get("reason", ""),
            "requested_changes": review.get("requested_changes") or [],
        }
        feedback = list(task.revision_feedback or []) + [entry]
        revision_count = task.revision_count + 1
        if revision_count >= MAX_TASK_REVISIONS:
            logger.warning(
                "Task %s exceeded max revisions (%d) on Tech Lead review; marking FAILED. Reason: %s",
                task.id, MAX_TASK_REVISIONS, entry["reason"],
            )
            self.graph.update_task(
                task.id,
                status=TaskStatus.FAILED,
                revision_count=revision_count,
                revision_feedback=feedback,
            )
            self._cascade_fail_dependents(task.id)
            return
        logger.info(
            "Task %s rejected by Tech Lead (revision %d); returning to engineer %s",
            task.id, revision_count, task.assigned_agent_id,
        )
        # Keep the assignment (do not clear assigned_agent_id / the agent->task mapping) so the
        # same engineer picks it up next round and revises the current work.
        self.graph.update_task(
            task.id,
            status=TaskStatus.IN_PROGRESS,
            revision_count=revision_count,
            revision_feedback=feedback,
        )

    def _fail_task(self, task: Task, review: Dict[str, Any], context: str) -> None:
        """Terminally fail a task (and its dependents) without spinning the revision loop.

        Used when a Tech Lead review cannot be performed (e.g. the review evidence exceeded the
        model context window). Re-routing such a task through the revision loop would re-send the
        same failing prompt every round up to MAX_TASK_REVISIONS at max cost; instead we record
        the diagnostic, mark the task FAILED, and cascade the failure to dependents.

        Postconditions:
            - task.status is FAILED; tasks transitively depending on it are FAILED too.
        """
        entry = {
            "source": "tech_lead",
            "reason": review.get("reason", context),
            "requested_changes": [],
        }
        feedback = list(task.revision_feedback or []) + [entry]
        logger.warning("%s for task %s; marking FAILED. Reason: %s", context, task.id, entry["reason"])
        self.graph.update_task(task.id, status=TaskStatus.FAILED, revision_feedback=feedback)
        self._cascade_fail_dependents(task.id)

    def _cascade_fail_dependents(self, task_id: str) -> None:
        """Propagate a task's FAILED state to every task that can no longer be satisfied.

        A task depending on a FAILED task can never satisfy `_dependencies_satisfied` (which
        requires MERGED deps), so without this it would sit TO_DO forever and keep the swarm loop
        from completing. Delegates to `TaskGraphService.mark_dependents_failed`.
        """
        blocked = self.graph.mark_dependents_failed(task_id)
        if blocked:
            logger.warning("Task %s failure cascaded FAILED to dependents: %s", task_id, blocked)

    def _is_complete(self) -> bool:
        tasks = self.graph.get_tasks()
        remaining = [t for t in tasks if t.status == TaskStatus.TO_DO]
        active = sum(1 for aid in self.agent_ids if self.graph.get_task_for_agent(aid) is not None)
        in_review = [t for t in tasks if t.status == TaskStatus.IN_REVIEW]
        return not remaining and active == 0 and not in_review

    def run(
        self,
        max_rounds: int = 500,
        check_cancel: Optional[Callable[[], bool]] = None,
        persist_fn: Optional[Callable] = None,
        update_fn: Optional[Callable] = None,
    ) -> None:
        """Main swarm loop: assign → implement + quality gates → review → merge."""
        _update = update_fn or (lambda **kw: None)
        _persist = persist_fn or (lambda: None)

        for round_num in range(max_rounds):
            if check_cancel and check_cancel():
                _update(status="cancelled", status_text="Cancelled by user")
                return

            # Refresh the repo context each round so the implement prompt reflects files written in
            # prior rounds (specs, plans, code). A one-time snapshot taken at construction makes a
            # worker blind to earlier work and recreate it.
            self.repo_context = _read_repo_context(self.path)

            # Coordinator: assign ready tasks to free workers
            ready = self._find_ready_tasks()
            free = self._find_free_agents()
            self._assign_tasks(ready, free)
            _persist()

            # Workers: implement + quality gates
            for swe in self.workers:
                self._implement_and_verify(swe, _update)
            _persist()

            # Coordinator: review and merge
            self._review_and_merge(_update)
            _persist()

            if self._is_complete():
                break
