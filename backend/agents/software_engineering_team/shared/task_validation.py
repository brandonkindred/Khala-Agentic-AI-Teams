"""
Task validation for Tech Lead output quality.

Structural validations only -- no hardcoded thresholds. LLM output is trusted.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from software_engineering_team.shared.models import ProductRequirements, Task, TaskAssignment


def validate_task(task: Task, valid_task_ids: set[str]) -> List[str]:
    """
    Validate a single task structurally. Returns list of validation error messages.
    """
    errors: List[str] = []

    if not task.id:
        errors.append("Task has empty id")

    for dep in task.dependencies or []:
        if dep and dep not in valid_task_ids:
            errors.append(f"Task {task.id}: Dependency '{dep}' references non-existent task")

    return errors


def validate_assignment(
    assignment: TaskAssignment,
    requirements: Optional[ProductRequirements] = None,
    requirement_task_mapping: Optional[list] = None,
) -> Tuple[bool, List[str]]:
    """
    Validate a TaskAssignment structurally. Returns (is_valid, list of error messages).
    """
    errors: List[str] = []
    valid_ids = {t.id for t in assignment.tasks}

    for tid in assignment.execution_order or []:
        if tid not in valid_ids:
            errors.append(f"Execution order references non-existent task: {tid}")

    ordered_set = set(assignment.execution_order or [])
    for t in assignment.tasks:
        if t.id not in ordered_set:
            errors.append(f"Task {t.id} not in execution_order")

    for task in assignment.tasks:
        errors.extend(validate_task(task, valid_ids))

    return len(errors) == 0, errors
