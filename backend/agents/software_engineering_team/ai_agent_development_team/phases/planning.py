"""Planning phase: create microtasks and assign tool agents."""

from __future__ import annotations

import json

from strands import Agent

from llm_service import get_strands_model
from software_engineering_team.shared.models import Task

from ..models import IntakeResult, Microtask, PlanningResult, ToolAgentKind
from ..prompts import PLANNING_PROMPT


def run_planning(
    *, llm=None, task: Task, intake_result: IntakeResult, spec_content: str
) -> PlanningResult:
    prompt = (
        f"Goal: {intake_result.system_goal}\n"
        f"Constraints: {intake_result.constraints}\n"
        f"Metrics: {intake_result.success_metrics}\n"
        f"Task: {task.description}\n"
        f"Spec:\n{(spec_content or '')[:7000]}"
    )
    from strands.models.model import Model as _StrandsModel

    _model = llm if (llm is not None and isinstance(llm, _StrandsModel)) else get_strands_model()
    agent = Agent(model=_model, system_prompt=PLANNING_PROMPT)
    result = agent(prompt)
    raw_text = str(result).strip()
    raw = json.loads(raw_text)
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
