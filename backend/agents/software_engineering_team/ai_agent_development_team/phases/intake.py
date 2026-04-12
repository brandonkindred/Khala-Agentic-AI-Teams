"""Intake phase: normalize mission and constraints from spec."""

from __future__ import annotations

import json

from strands import Agent

from llm_service import get_strands_model
from software_engineering_team.shared.models import Task

from ..models import IntakeResult
from ..prompts import INTAKE_PROMPT


def run_intake(*, llm=None, task: Task, spec_content: str) -> IntakeResult:
    prompt = (
        f"Task title: {task.title or task.id}\n"
        f"Task description: {task.description}\n"
        f"Requirements: {task.requirements}\n"
        f"Acceptance criteria: {task.acceptance_criteria}\n"
        f"Spec:\n{(spec_content or '')[:8000]}"
    )
    agent = Agent(model=get_strands_model(), system_prompt=INTAKE_PROMPT)
    result = agent(prompt)
    raw_text = (result.message if hasattr(result, "message") else str(result)).strip()
    raw = json.loads(raw_text)
    return IntakeResult(
        system_goal=raw.get("system_goal", ""),
        constraints=raw.get("constraints") or [],
        risks=raw.get("risks") or [],
        success_metrics=raw.get("success_metrics") or [],
        summary=raw.get("summary", ""),
    )
