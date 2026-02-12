"""
Task validation for Tech Lead output quality.

Validates that tasks are well-defined and sufficient for delivery.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from shared.models import ProductRequirements, Task, TaskAssignment, TaskType


# Minimum description length (chars) for a well-defined task
MIN_DESCRIPTION_LENGTH = 50

# Minimum requirements length for coding tasks
MIN_REQUIREMENTS_LENGTH = 10

# Minimum acceptance criteria for coding tasks (backend, frontend, devops)
MIN_ACCEPTANCE_CRITERIA_COUNT = 3

# Minimum tasks for non-trivial apps (git_setup + devops + backend + frontend only; no security/qa)
MIN_TASKS_NON_TRIVIAL = 8

# Minimum tasks for trivial apps (minimal spec)
MIN_TASKS_TRIVIAL = 4

# Coding task types that require detailed acceptance criteria
CODING_TASK_TYPES = {TaskType.BACKEND, TaskType.FRONTEND, TaskType.DEVOPS}

# Task types that require a user story
USER_STORY_REQUIRED_TYPES = {TaskType.BACKEND, TaskType.FRONTEND}


def validate_task(task: Task, valid_task_ids: set[str]) -> List[str]:
    """
    Validate a single task. Returns list of validation error messages.
    """
    errors: List[str] = []

    # Descriptive ID (no t1, t2, t3)
    if task.id and len(task.id) <= 3 and task.id.startswith("t"):
        errors.append(f"Task {task.id}: Use descriptive kebab-case IDs (e.g. backend-todo-crud-api), not t1/t2/t3")

    # Title must be non-empty for coding tasks
    if task.type in CODING_TASK_TYPES:
        title = (task.title or "").strip()
        if not title:
            errors.append(f"Task {task.id}: Missing 'title' (descriptive title) for {task.type.value} task")

    # Description must be substantial
    desc = (task.description or "").strip()
    if len(desc) < MIN_DESCRIPTION_LENGTH:
        errors.append(f"Task {task.id}: Description too short ({len(desc)} chars, min {MIN_DESCRIPTION_LENGTH}). Provide an in-depth, outcomes-based description.")

    # User story required for backend/frontend tasks
    if task.type in USER_STORY_REQUIRED_TYPES:
        user_story = (task.user_story or "").strip()
        if not user_story:
            errors.append(f"Task {task.id}: Missing 'user_story' for {task.type.value} task. Format: 'As a [role], I want [goal] so that [benefit]'")

    # Coding tasks need requirements
    if task.type in CODING_TASK_TYPES:
        reqs = (task.requirements or "").strip()
        if len(reqs) < MIN_REQUIREMENTS_LENGTH:
            errors.append(f"Task {task.id}: Requirements too short or missing for {task.type.value} task")

        # Coding tasks need at least MIN_ACCEPTANCE_CRITERIA_COUNT acceptance criteria
        if len(task.acceptance_criteria or []) < MIN_ACCEPTANCE_CRITERIA_COUNT:
            errors.append(f"Task {task.id}: Needs at least {MIN_ACCEPTANCE_CRITERIA_COUNT} acceptance criteria (has {len(task.acceptance_criteria or [])})")

    # Dependencies must reference valid task IDs
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
    Validate a TaskAssignment. Returns (is_valid, list of error messages).
    """
    errors: List[str] = []
    valid_ids = {t.id for t in assignment.tasks}

    # All tasks in execution_order must exist
    for tid in assignment.execution_order or []:
        if tid not in valid_ids:
            errors.append(f"Execution order references non-existent task: {tid}")

    # Every task must appear in execution_order
    ordered_set = set(assignment.execution_order or [])
    for t in assignment.tasks:
        if t.id not in ordered_set:
            errors.append(f"Task {t.id} not in execution_order")

    # Validate each task
    for task in assignment.tasks:
        errors.extend(validate_task(task, valid_ids))

    # Minimum task count
    is_trivial = requirements and (
        len(requirements.description or "") < 500
        or len(requirements.acceptance_criteria or []) < 3
    )
    min_tasks = MIN_TASKS_TRIVIAL if is_trivial else MIN_TASKS_NON_TRIVIAL
    coding_count = sum(1 for t in assignment.tasks if t.type in CODING_TASK_TYPES)
    if coding_count > 0 and len(assignment.tasks) < min_tasks:
        errors.append(
            f"Too few tasks: {len(assignment.tasks)} (expected at least {min_tasks} for {'trivial' if is_trivial else 'non-trivial'} spec)"
        )

    # Spec coverage: when mapping is provided, each acceptance criterion must be mapped to at least one task
    if requirements and (requirements.acceptance_criteria or []) and requirement_task_mapping:
        mapped_spec_items = set()
        for item in requirement_task_mapping or []:
            if isinstance(item, dict):
                spec_item = item.get("spec_item") or item.get("requirement") or ""
                task_ids = item.get("task_ids") or []
                if spec_item and task_ids:
                    mapped_spec_items.add(spec_item.strip().lower())
        for ac in requirements.acceptance_criteria or []:
            ac_normalized = ac.strip().lower()
            # Check if any mapped item is a substring match or vice versa
            if not any(
                ac_normalized in mapped or mapped in ac_normalized
                for mapped in mapped_spec_items
            ):
                errors.append(f"Acceptance criterion not mapped to tasks: '{ac[:60]}...'")

    return len(errors) == 0, errors
