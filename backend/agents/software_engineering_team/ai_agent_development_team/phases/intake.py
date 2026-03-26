"""Intake phase: normalize mission and constraints from spec."""

from __future__ import annotations

from llm_service import LLMClient
from software_engineering_team.shared.models import Task

from ..models import IntakeResult
from ..prompts import INTAKE_PROMPT


def run_intake(*, llm: LLMClient, task: Task, spec_content: str) -> IntakeResult:
    prompt = (
        f"{INTAKE_PROMPT}\n\n"
        f"Task title: {task.title or task.id}\n"
        f"Task description: {task.description}\n"
        f"Requirements: {task.requirements}\n"
        f"Acceptance criteria: {task.acceptance_criteria}\n"
        f"Spec:\n{(spec_content or '')[:8000]}"
    )
    raw = llm.complete_json(prompt, think=True)
    return IntakeResult(
        system_goal=raw.get("system_goal", ""),
        constraints=raw.get("constraints") or [],
        risks=raw.get("risks") or [],
        success_metrics=raw.get("success_metrics") or [],
        summary=raw.get("summary", ""),
    )
