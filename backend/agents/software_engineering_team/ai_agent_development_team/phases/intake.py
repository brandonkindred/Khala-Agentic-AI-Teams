"""Intake phase: normalize mission and constraints from spec."""

from __future__ import annotations

from shared.dev_models.models import Task
from software_engineering_team.shared.llm import complete_json_with_continuation

from ..models import IntakeResult
from ..prompts import intake_system_prompt


def run_intake(*, llm=None, task: Task, spec_content: str) -> IntakeResult:
    """Normalize the task and spec into a mission brief via the intake LLM prompt.

    Preconditions:
        ``task`` is a valid ``Task``. ``spec_content`` may be empty or ``None``.
        ``llm`` is a Strands ``Model``, an ``LLMClient``, or ``None``.
    Postconditions:
        Returns an ``IntakeResult`` built from the parsed JSON response, with
        missing fields defaulting to an empty string or empty list.

    Raises:
        ValueError: the LLM response parsed to a non-object JSON value (e.g.
            a bare array) instead of the expected object.
    """
    prompt = (
        f"Task title: {task.title or task.id}\n"
        f"Task description: {task.description}\n"
        f"Requirements: {task.requirements}\n"
        f"Acceptance criteria: {task.acceptance_criteria}\n"
        f"Spec:\n{(spec_content or '')}"
    )
    raw = complete_json_with_continuation(llm, prompt, system_prompt=intake_system_prompt())
    if not isinstance(raw, dict):
        raise ValueError(f"Intake LLM response is not a JSON object (got {type(raw).__name__})")
    return IntakeResult(
        system_goal=raw.get("system_goal", ""),
        constraints=raw.get("constraints") or [],
        risks=raw.get("risks") or [],
        success_metrics=raw.get("success_metrics") or [],
        summary=raw.get("summary", ""),
    )
