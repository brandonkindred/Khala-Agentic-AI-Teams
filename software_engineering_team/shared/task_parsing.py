"""
Parse LLM JSON output into TaskAssignment.

Shared by Tech Lead and Task Generator agents.
"""

from __future__ import annotations

from typing import Any, Dict, List

from shared.models import Task, TaskAssignment, TaskStatus, TaskType


def parse_assignment_from_data(data: Dict[str, Any]) -> TaskAssignment:
    """
    Parse LLM JSON output into TaskAssignment.
    Filters out security/qa (orchestrator invokes those).
    """
    tasks: List[Task] = []
    for t in data.get("tasks") or []:
        if isinstance(t, dict) and t.get("id"):
            assignee = t.get("assignee") or "devops"
            try:
                task_type = TaskType(t.get("type", "backend"))
            except ValueError:
                task_type = TaskType.BACKEND
            if task_type in (TaskType.SECURITY, TaskType.QA):
                continue
            acc = t.get("acceptance_criteria") or []
            if not isinstance(acc, list):
                acc = [str(acc)] if acc else []
            tasks.append(
                Task(
                    id=t["id"],
                    type=task_type,
                    title=t.get("title") or t.get("name") or t.get("label", ""),
                    description=t.get("description", ""),
                    user_story=t.get("user_story", ""),
                    assignee=assignee,
                    requirements=t.get("requirements", ""),
                    dependencies=t.get("dependencies", []),
                    acceptance_criteria=acc,
                    status=TaskStatus.PENDING,
                )
            )

    execution_order = data.get("execution_order") or [t.id for t in tasks]
    valid_ids = {t.id for t in tasks}
    execution_order = [tid for tid in execution_order if tid in valid_ids]
    execution_order = _interleave_execution_order(execution_order, {t.id: t for t in tasks})

    return TaskAssignment(
        tasks=tasks,
        execution_order=execution_order,
        rationale=data.get("rationale", ""),
    )


def _interleave_execution_order(
    execution_order: List[str],
    tasks_by_id: Dict[str, Any],
) -> List[str]:
    """
    Interleave backend and frontend tasks while respecting dependencies.
    Only adds a task when all its dependencies are already in the result.
    Prefers alternating backend/frontend when multiple candidates are runnable.
    """
    prefix: List[str] = []
    backend_list: List[str] = []
    frontend_list: List[str] = []

    for tid in execution_order:
        task = tasks_by_id.get(tid)
        if not task:
            prefix.append(tid)
            continue
        assignee = getattr(task, "assignee", None) or ""
        if assignee == "backend":
            backend_list.append(tid)
        elif assignee == "frontend":
            frontend_list.append(tid)
        else:
            prefix.append(tid)

    result: List[str] = list(prefix)
    added: set = set(result)

    def is_runnable(tid: str) -> bool:
        task = tasks_by_id.get(tid)
        if not task:
            return True
        deps = getattr(task, "dependencies", None) or []
        return all(dep in added for dep in deps)

    last_was_backend: bool | None = None
    while True:
        backend_ready = [t for t in backend_list if t not in added and is_runnable(t)]
        frontend_ready = [t for t in frontend_list if t not in added and is_runnable(t)]

        candidate: str | None = None
        if last_was_backend is False and frontend_ready:
            candidate = frontend_ready[0]
        elif last_was_backend is True and backend_ready:
            candidate = backend_ready[0]
        elif backend_ready:
            candidate = backend_ready[0]
        elif frontend_ready:
            candidate = frontend_ready[0]

        if candidate is None:
            break

        result.append(candidate)
        added.add(candidate)
        task = tasks_by_id.get(candidate)
        assignee = getattr(task, "assignee", None) or ""
        last_was_backend = assignee == "backend"

    # Fallback: add any remaining (e.g. circular deps, missing deps) in original order
    for tid in backend_list + frontend_list:
        if tid not in added:
            result.append(tid)

    return result
