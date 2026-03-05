"""Shared helpers for building task requirement strings.

Consolidates ``_task_requirements``, ``_task_requirements_with_test_expectations``,
and ``_task_requirements_with_route_expectations`` that were previously duplicated
across backend_agent, orchestrator, and frontend_team modules.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List

from software_engineering_team.shared.models import Task

logger = logging.getLogger(__name__)


def task_requirements(task: Task) -> str:
    """Build a full requirements string from a :class:`Task` object.

    Combines description, user story, technical requirements, and acceptance
    criteria into a single prompt-ready string.
    """
    parts: List[str] = []
    if task.description:
        parts.append(f"Task Description:\n{task.description}")
    if getattr(task, "user_story", None):
        parts.append(f"User Story: {task.user_story}")
    if task.requirements:
        parts.append(f"Technical Requirements:\n{task.requirements}")
    if getattr(task, "acceptance_criteria", None):
        parts.append("Acceptance Criteria:\n- " + "\n- ".join(task.acceptance_criteria))
    return "\n\n".join(parts) if parts else task.description


def task_requirements_with_expectations(
    task: Task,
    repo_path: Path,
    domain: str,
) -> str:
    """Build requirements string augmented with test/spec expectations.

    Parameters
    ----------
    task:
        The task to build requirements for.
    repo_path:
        Path to the repository root.
    domain:
        ``"backend"`` or ``"frontend"`` — determines which checklist to load.
    """
    base = task_requirements(task)
    try:
        from software_engineering_team.shared.test_spec_expectations import build_test_spec_checklist
        checklist = build_test_spec_checklist(repo_path, domain)
        if checklist:
            base += "\n\n" + checklist
    except (ImportError, FileNotFoundError):
        pass
    return base
