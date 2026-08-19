"""Shared helpers for building task requirement strings.

Consolidates ``_task_requirements``, ``_task_requirements_with_test_expectations``,
and ``_task_requirements_with_route_expectations`` that were previously duplicated
across backend_agent and orchestrator modules.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List

from shared.dev_models.models import Task

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
        from software_engineering_team.shared.test_spec_expectations import (
            build_test_spec_checklist,
        )

        checklist = build_test_spec_checklist(repo_path, domain)
        if checklist:
            base += "\n\n" + checklist
    except (ImportError, FileNotFoundError):
        pass
    return base


def merge_extra_requirements(base: str, extra: str) -> str:
    """Merge an optional extra requirements clause into a base requirements string.

    Single source of truth for the blank-line-separator-or-verbatim merge rule
    shared by ``shared.v2_review.run_coordinator_llm_review``'s
    ``extra_task_requirements`` handling and
    ``shared.v2_orchestrator.ConfigDrivenV2DevelopmentAgent.build_task_requirements``,
    so the two can no longer diverge on how a clause (e.g. frontend's
    accessibility-verification note) gets appended.

    Preconditions: ``base`` and ``extra`` are strings (either may be empty).
    Postconditions: returns ``base`` unchanged when ``extra`` is ``""``;
      otherwise returns ``extra`` appended after a blank-line separator when
      ``base`` is non-empty, or ``extra`` verbatim when ``base`` is empty.
      Pure; no side effects.
    """
    if not extra:
        return base
    if base:
        return f"{base}\n\n{extra}"
    return extra
