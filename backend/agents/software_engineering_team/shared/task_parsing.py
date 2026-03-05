"""
Parse LLM JSON output into TaskAssignment and PlanningHierarchy.

Trusts LLM output directly -- no heuristic normalization or reordering.
"""

from __future__ import annotations

from typing import Any, Dict, List

from software_engineering_team.shared.models import (
    Epic,
    Initiative,
    PlanningHierarchy,
    StoryPlan,
    Task,
    TaskPlan,
    TaskAssignment,
    TaskStatus,
    TaskType,
)


def parse_hierarchy_from_data(data: Dict[str, Any]) -> PlanningHierarchy:
    """
    Parse LLM JSON output into a PlanningHierarchy (Initiative -> Epic -> Story -> Task).
    Stories may contain a "tasks" array; each task becomes an assignable unit when flattened.
    """
    initiatives: List[Initiative] = []
    for init_data in data.get("initiatives") or []:
        if not isinstance(init_data, dict):
            continue
        epics: List[Epic] = []
        for epic_data in init_data.get("epics") or []:
            if not isinstance(epic_data, dict):
                continue
            stories: List[StoryPlan] = []
            for story_data in epic_data.get("stories") or []:
                if not isinstance(story_data, dict) or not story_data.get("id"):
                    continue
                acc = story_data.get("acceptance_criteria") or []
                if not isinstance(acc, list):
                    acc = [str(acc)] if acc else []
                task_plans: List[TaskPlan] = []
                for task_data in story_data.get("tasks") or []:
                    if not isinstance(task_data, dict) or not task_data.get("id"):
                        continue
                    t_acc = task_data.get("acceptance_criteria") or []
                    if not isinstance(t_acc, list):
                        t_acc = [str(t_acc)] if t_acc else []
                    task_plans.append(TaskPlan(
                        id=task_data["id"],
                        title=task_data.get("title") or "",
                        description=task_data.get("description") or "",
                        user_story=task_data.get("user_story") or "",
                        assignee=task_data.get("assignee") or "backend",
                        requirements=task_data.get("requirements") or "",
                        dependencies=task_data.get("dependencies") or [],
                        acceptance_criteria=t_acc,
                        example=task_data.get("example"),
                        metadata=task_data.get("metadata") or {},
                    ))
                stories.append(StoryPlan(
                    id=story_data["id"],
                    title=story_data.get("title") or "",
                    description=story_data.get("description") or "",
                    user_story=story_data.get("user_story") or "",
                    requirements=story_data.get("requirements") or "",
                    acceptance_criteria=acc,
                    example=story_data.get("example"),
                    tasks=task_plans,
                    assignee=story_data.get("assignee") if not task_plans else None,
                    dependencies=story_data.get("dependencies") or [],
                    metadata=story_data.get("metadata") or {},
                ))
            acc = epic_data.get("acceptance_criteria") or []
            if not isinstance(acc, list):
                acc = [str(acc)] if acc else []
            epics.append(Epic(
                id=epic_data.get("id") or "",
                title=epic_data.get("title") or "",
                description=epic_data.get("description") or "",
                user_stories_summary=epic_data.get("user_stories_summary") or [],
                acceptance_criteria=acc,
                stories=stories,
            ))
        initiatives.append(Initiative(
            id=init_data.get("id") or "",
            title=init_data.get("title") or "",
            description=init_data.get("description") or "",
            epics=epics,
        ))

    execution_order = data.get("execution_order") or []
    return PlanningHierarchy(
        initiatives=initiatives,
        execution_order=execution_order,
        rationale=data.get("rationale") or "",
    )


def flatten_hierarchy_to_assignment(hierarchy: PlanningHierarchy) -> TaskAssignment:
    """
    Flatten a PlanningHierarchy into a TaskAssignment for the execution layer.
    Each Task under a Story becomes one Task (distributed to backend/frontend/devops).
    If a story has no tasks, the story itself is treated as a single task (backward compat).
    """
    tasks: List[Task] = []
    seen: set = set()
    for initiative in hierarchy.initiatives:
        for epic in initiative.epics:
            for story in epic.stories:
                if story.tasks:
                    for tp in story.tasks:
                        if tp.id in seen:
                            continue
                        seen.add(tp.id)
                        task_type = _assignee_to_task_type(tp.assignee)
                        tasks.append(Task(
                            id=tp.id,
                            type=task_type,
                            title=tp.title,
                            description=tp.description,
                            user_story=tp.user_story,
                            assignee=tp.assignee,
                            requirements=tp.requirements,
                            dependencies=tp.dependencies,
                            acceptance_criteria=tp.acceptance_criteria,
                            status=TaskStatus.PENDING,
                            metadata={
                                "epic_id": epic.id,
                                "initiative_id": initiative.id,
                                "story_id": story.id,
                                "example": tp.example,
                                **(tp.metadata or {}),
                            },
                        ))
                else:
                    # Backward compat: story with no tasks → one task from story
                    assignee = story.assignee or "backend"
                    if story.id in seen:
                        continue
                    seen.add(story.id)
                    task_type = _assignee_to_task_type(assignee)
                    tasks.append(Task(
                        id=story.id,
                        type=task_type,
                        title=story.title,
                        description=story.description,
                        user_story=story.user_story,
                        assignee=assignee,
                        requirements=story.requirements,
                        dependencies=story.dependencies,
                        acceptance_criteria=story.acceptance_criteria,
                        status=TaskStatus.PENDING,
                        metadata={
                            "epic_id": epic.id,
                            "initiative_id": initiative.id,
                            "story_id": story.id,
                            **(story.metadata or {}),
                        },
                    ))

    valid_ids = {t.id for t in tasks}
    execution_order = [tid for tid in hierarchy.execution_order if tid in valid_ids]
    for t in tasks:
        if t.id not in execution_order:
            execution_order.append(t.id)

    return TaskAssignment(
        tasks=tasks,
        execution_order=execution_order,
        rationale=hierarchy.rationale,
    )


def parse_assignment_from_data(data: Dict[str, Any]) -> TaskAssignment:
    """
    Parse LLM JSON output into TaskAssignment.

    Supports two formats:
    1. Hierarchical (initiatives -> epics -> stories) -- preferred
    2. Flat (tasks list) -- backward compatibility

    No heuristic normalization is applied. LLM output is trusted directly.
    """
    if data.get("initiatives"):
        hierarchy = parse_hierarchy_from_data(data)
        return flatten_hierarchy_to_assignment(hierarchy)

    tasks: List[Task] = []
    for t in data.get("tasks") or []:
        if isinstance(t, dict) and t.get("id"):
            assignee = t.get("assignee") or "backend"
            task_type = _assignee_to_task_type(assignee)
            if t.get("type"):
                try:
                    task_type = TaskType(t["type"])
                except ValueError:
                    pass
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
                    metadata=t.get("metadata") or {},
                )
            )

    execution_order = data.get("execution_order") or [t.id for t in tasks]
    valid_ids = {t.id for t in tasks}
    execution_order = [tid for tid in execution_order if tid in valid_ids]

    return TaskAssignment(
        tasks=tasks,
        execution_order=execution_order,
        rationale=data.get("rationale", ""),
    )


def _assignee_to_task_type(assignee: str) -> TaskType:
    """Map assignee string to TaskType."""
    mapping = {
        "backend": TaskType.BACKEND,
        "backend-code-v2": TaskType.BACKEND,
        "frontend": TaskType.FRONTEND,
        "frontend-code-v2": TaskType.FRONTEND,
        "devops": TaskType.DEVOPS,
    }
    return mapping.get(assignee, TaskType.BACKEND)


