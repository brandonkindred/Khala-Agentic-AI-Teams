"""Planning phase: create microtasks and assign tool agents."""

from __future__ import annotations

from shared.llm import LLMClient
from shared.models import Task

from ..models import IntakeResult, Microtask, PlanningResult, ToolAgentKind
from ..prompts import PLANNING_PROMPT


def run_planning(*, llm: LLMClient, task: Task, intake_result: IntakeResult, spec_content: str) -> PlanningResult:
    prompt = (
        f"{PLANNING_PROMPT}\n\n"
        f"Goal: {intake_result.system_goal}\n"
        f"Constraints: {intake_result.constraints}\n"
        f"Metrics: {intake_result.success_metrics}\n"
        f"Task: {task.description}\n"
        f"Spec:\n{(spec_content or '')[:7000]}"
    )
    raw = llm.complete_json(prompt)
    microtasks = []
    for item in raw.get("microtasks") or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        try:
            kind = ToolAgentKind(item.get("tool_agent", "general"))
        except ValueError:
            kind = ToolAgentKind.GENERAL
        microtasks.append(
            Microtask(
                id=item["id"],
                title=item.get("title", ""),
                description=item.get("description", ""),
                tool_agent=kind,
                depends_on=item.get("depends_on") or [],
            )
        )
    if not microtasks:
        microtasks = [
            Microtask(
                id="mt-agent-blueprint",
                title="Create baseline agent blueprint",
                description="Generate the first-cut multi-agent design and implementation artifacts.",
                tool_agent=ToolAgentKind.GENERAL,
            )
        ]
    return PlanningResult(microtasks=microtasks, summary=raw.get("summary", ""))
