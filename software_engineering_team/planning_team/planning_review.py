"""
Planning review: alignment between tasks and architecture, and conformance to initial spec.

Used by the orchestrator to implement the planning loop:
- Tasks and architecture must align (tasks support architecture and vice versa).
- Tasks and architecture must conform to the initial_spec; if not, planning is re-run with feedback.
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional, Tuple

from shared.models import TaskAssignment, SystemArchitecture

logger = logging.getLogger(__name__)

ALIGNMENT_PROMPT = """You are a planning reviewer. Given a task list and a system architecture, determine whether they are aligned.

**Tasks (summary):**
{tasks_summary}

**Architecture:**
{architecture_summary}

**Your task:** Decide if (1) the tasks fully support implementing the architecture (every component/capability has corresponding tasks), and (2) the architecture supports the tasks (no tasks require capabilities or components missing from the architecture).

Respond with a JSON object only:
- "aligned": true or false
- "feedback": list of strings describing gaps (e.g. "Architecture has component X but no task implements it", "Task Y requires Z but architecture does not include Z"). Empty list if aligned is true.
"""

CONFORMANCE_PROMPT = """You are a spec conformance reviewer. Given the initial specification, a task list, and a system architecture, determine whether the tasks and architecture conform to the spec.

**Initial specification:**
{spec_excerpt}

**Tasks (summary):**
{tasks_summary}

**Architecture overview and components:**
{architecture_summary}

**Your task:** List any way the tasks or the architecture do NOT conform to the initial spec (e.g. missing features, wrong scope, violated constraints). If everything conforms, return conformant true and an empty issues list.

Respond with a JSON object only:
- "conformant": true or false
- "issues": list of strings, each describing one non-compliance. Empty list if conformant is true.
"""


def _tasks_summary(assignment: TaskAssignment, max_tasks: int = 80) -> str:
    """Build a short text summary of the task assignment for prompts."""
    lines = []
    for i, t in enumerate(assignment.tasks[:max_tasks]):
        lines.append(f"- [{t.id}] ({getattr(t.type, 'value', t.type)} / {t.assignee}) {t.title or t.description[:80]}")
    if len(assignment.tasks) > max_tasks:
        lines.append(f"... and {len(assignment.tasks) - max_tasks} more tasks")
    return "\n".join(lines) if lines else "(No tasks)"


def _architecture_summary(arch: SystemArchitecture, max_components: int = 30) -> str:
    """Build a short text summary of the architecture for prompts."""
    parts = [arch.overview.strip() or "(No overview)"]
    if arch.components:
        parts.append("\nComponents:")
        for c in arch.components[:max_components]:
            parts.append(f"- {c.name} ({c.type}): {c.description[:120] or 'N/A'}")
    return "\n".join(parts)


def check_tasks_architecture_alignment(
    assignment: TaskAssignment,
    architecture: SystemArchitecture,
    llm: Any,
) -> Tuple[bool, List[str]]:
    """
    Check whether the task list and architecture are aligned: tasks support the architecture
    and the architecture supports the tasks. Returns (aligned, feedback list).
    """
    tasks_summary = _tasks_summary(assignment)
    architecture_summary = _architecture_summary(architecture)
    prompt = ALIGNMENT_PROMPT.format(
        tasks_summary=tasks_summary,
        architecture_summary=architecture_summary,
    )
    try:
        data = llm.complete_json(prompt, temperature=0.1)
    except Exception as e:
        logger.warning("Alignment check LLM call failed: %s; assuming aligned", e)
        return True, []

    aligned = bool(data.get("aligned", True))
    feedback = data.get("feedback") or []
    if not isinstance(feedback, list):
        feedback = [str(feedback)] if feedback else []
    return aligned, feedback


def check_spec_conformance(
    spec_content: str,
    assignment: TaskAssignment,
    architecture: SystemArchitecture,
    llm: Any,
    spec_excerpt_max: int = 12000,
) -> Tuple[bool, List[str]]:
    """
    Check whether the tasks and architecture conform to the initial spec.
    Returns (conformant, list of non-compliance issues). If conformant is False, issues list
    should be passed back to the task/planning step as feedback.
    """
    spec_excerpt = (spec_content or "").strip()[:spec_excerpt_max]
    if len(spec_content or "") > spec_excerpt_max:
        spec_excerpt += "\n... (spec truncated)"
    tasks_summary = _tasks_summary(assignment)
    architecture_summary = _architecture_summary(architecture)
    prompt = CONFORMANCE_PROMPT.format(
        spec_excerpt=spec_excerpt,
        tasks_summary=tasks_summary,
        architecture_summary=architecture_summary,
    )
    try:
        data = llm.complete_json(prompt, temperature=0.1)
    except Exception as e:
        logger.warning("Conformance check LLM call failed: %s; assuming conformant", e)
        return True, []

    conformant = bool(data.get("conformant", True))
    issues = data.get("issues") or []
    if not isinstance(issues, list):
        issues = [str(issues)] if issues else []
    return conformant, issues
