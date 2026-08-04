"""Planning phase: create microtasks and assign tool agents."""

from __future__ import annotations

from software_engineering_team.shared.llm import complete_json_with_continuation
from software_engineering_team.shared.models import Task

from ..models import IntakeResult, Microtask, PlanningResult, ToolAgentKind
from ..prompts import planning_system_prompt


def run_planning(
    *, llm=None, task: Task, intake_result: IntakeResult, spec_content: str
) -> PlanningResult:
    """Derive an ordered microtask plan from the intake result and spec.

    Preconditions:
        ``task`` is a valid ``Task``. ``intake_result`` is the ``IntakeResult``
        produced by the prior phase. ``spec_content`` may be empty or ``None``.
        ``llm`` is a Strands ``Model``, an ``LLMClient``, or ``None``.
    Postconditions:
        Returns a ``PlanningResult`` whose ``microtasks`` are built from the
        parsed JSON response's ``microtasks`` list, skipping entries that are
        not a dict or lack an ``id`` and coercing an unrecognized
        ``tool_agent`` to ``ToolAgentKind.GENERAL``. If no entry survives
        filtering, returns a single baseline blueprint microtask so the
        result is never empty.

    Raises:
        ValueError: the LLM response parsed to a non-object JSON value (e.g.
            a bare array) instead of the expected object.
    """
    prompt = (
        f"Goal: {intake_result.system_goal}\n"
        f"Constraints: {intake_result.constraints}\n"
        f"Metrics: {intake_result.success_metrics}\n"
        f"Task: {task.description}\n"
        f"Spec:\n{(spec_content or '')}"
    )
    raw = complete_json_with_continuation(llm, prompt, system_prompt=planning_system_prompt())
    if not isinstance(raw, dict):
        raise ValueError(f"Planning LLM response is not a JSON object (got {type(raw).__name__})")
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
