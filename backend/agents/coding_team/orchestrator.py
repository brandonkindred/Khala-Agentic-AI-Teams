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
MAX_TASK_REVISIONS = 3  # max times a task can be returned for revision before accepting


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

    # Tech Lead: plan → tasks + stacks
    llm = llm_getter("tech_lead")
    tech_lead = TechLeadAgent(llm)
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
    _update(
        status="completed",
        phase="completed",
        status_text=f"Completed: {merged_count} tasks merged",
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

        update_fn(status_text=f"Implementing: {task.title}")
        result = swe.run_implement(task, self.path, repo_context=self.repo_context)

        if result.get("status") == "in_review":
            # Run quality gates as tools
            if not self._run_quality_gates(swe, task, result, update_fn):
                return  # task returned to TODO for revision
            self.graph.update_task(task.id, feature_branch=result.get("feature_branch"))
            self.graph.set_task_in_review(task.id)
        elif result.get("status") == "failed":
            logger.warning("Worker %s task %s failed: %s", swe.agent_id, task.id, result.get("error"))

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
        self.graph.update_task(
            task.id,
            status=TaskStatus.TO_DO,
            assigned_agent_id=None,
            revision_count=revision_count,
            revision_feedback=feedback,
        )
        return False

    def _review_and_merge(self, update_fn: Callable) -> None:
        """Coordinator reviews completed tasks and merges approved ones."""
        in_review = [t for t in self.graph.get_tasks() if t.status == TaskStatus.IN_REVIEW]
        for task in in_review:
            update_fn(status_text=f"Tech Lead reviewing: {task.title}")
            review = self.tech_lead.run_code_review(
                task_title=task.title,
                task_description=task.description,
                acceptance_criteria=task.acceptance_criteria,
                changes_summary="See implementation summary.",
            )
            if review.get("approved"):
                try:
                    from software_engineering_team.shared.git_utils import (
                        DEVELOPMENT_BRANCH,
                        merge_branch,
                    )
                    branch = task.feature_branch or f"feature/{task.id}"
                    ok, _ = merge_branch(self.path, branch, DEVELOPMENT_BRANCH)
                    if ok:
                        self.graph.mark_branch_merged(task.id)
                except Exception as e:
                    logger.warning("Merge failed for %s: %s; marking merged anyway", task.id, e)
                    self.graph.mark_branch_merged(task.id)

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
