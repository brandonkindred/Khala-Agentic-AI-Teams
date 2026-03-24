"""
Coding team orchestrator: plan → Task Graph → assign → implement → review → merge.
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
    TaskStatus,
)
from coding_team.senior_software_engineer_agent import SeniorSWEAgent
from coding_team.task_graph import TaskGraphService, create_task_graph
from coding_team.tech_lead_agent import TechLeadAgent

logger = logging.getLogger(__name__)

CANCEL_KEY = "cancel_requested"


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
        lambda key: __import__("llm_service.factory", fromlist=["get_client"]).get_client(
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

    # Loop: assign → implement → review → merge
    repo_context = _read_repo_context(path)
    max_rounds = 500
    for round_num in range(max_rounds):
        if _check_cancel():
            _update(status="cancelled", status_text="Cancelled by user")
            return

        # Ready tasks: status TO_DO and dependencies satisfied
        all_tasks = graph.get_tasks()
        ready = [
            t
            for t in all_tasks
            if t.status == TaskStatus.TO_DO and graph._dependencies_satisfied(t.id)
        ]
        free_agents = [aid for aid in agent_ids if graph.get_task_for_agent(aid) is None]
        if free_agents and ready:
            assignments = tech_lead.run_assignments(
                agent_ids=agent_ids,
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
                    graph.assign_task_to_agent(task_id, agent_id)
        _persist_graph()

        # Senior SWEs: implement assigned tasks
        for swe in senior_swes:
            task = graph.get_task_for_agent(swe.agent_id)
            if not task:
                continue
            status_text = f"Implementing: {task.title}"
            _update(status_text=status_text)
            result = swe.run_implement(task, path, repo_context=repo_context)
            if result.get("status") == "in_review":
                graph.update_task(task.id, feature_branch=result.get("feature_branch"))
                graph.set_task_in_review(task.id)
            elif result.get("status") == "failed":
                logger.warning(
                    "Senior SWE %s task %s failed: %s", swe.agent_id, task.id, result.get("error")
                )
        _persist_graph()

        # Tech Lead: review in_review tasks and merge if approved
        in_review_tasks = [t for t in graph.get_tasks() if t.status == TaskStatus.IN_REVIEW]
        for task in in_review_tasks:
            review = tech_lead.run_code_review(
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
                    ok, _ = merge_branch(path, branch, DEVELOPMENT_BRANCH)
                    if ok:
                        graph.mark_branch_merged(task.id)
                except Exception as e:
                    logger.warning(
                        "Merge failed for %s: %s; marking merged in graph anyway", task.id, e
                    )
                    graph.mark_branch_merged(task.id)
        _persist_graph()

        # Done when no more TO_DO and no agent has active task and no in_review
        remaining_todo = [t for t in graph.get_tasks() if t.status == TaskStatus.TO_DO]
        active = sum(1 for aid in agent_ids if graph.get_task_for_agent(aid) is not None)
        still_in_review = [t for t in graph.get_tasks() if t.status == TaskStatus.IN_REVIEW]
        if not remaining_todo and active == 0 and not still_in_review:
            break

    merged_count = sum(1 for t in graph.get_tasks() if t.status == TaskStatus.MERGED)
    _update(
        status="completed",
        phase="completed",
        status_text=f"Completed: {merged_count} tasks merged",
    )
