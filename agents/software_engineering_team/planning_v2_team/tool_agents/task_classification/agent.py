"""
Task Classification tool agent for planning-v2.

Participates in phases: Implementation only.
Classifies tasks into the right execution teams (frontend, backend, devops, qa).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from shared.models import PlanningHierarchy

from ...models import ToolAgentPhaseInput, ToolAgentPhaseOutput
from ..json_utils import parse_json_with_recovery

if TYPE_CHECKING:
    from shared.llm import LLMClient

logger = logging.getLogger(__name__)

TASK_CLASSIFICATION_PROMPT = """You are a Task Classification expert. Classify these tasks into execution teams.

Tasks to classify:
---
{tasks}
---

For each task, determine the appropriate execution team:
- frontend: UI components, user interactions, styling, client-side logic
- backend: API endpoints, business logic, database operations, server-side processing
- devops: CI/CD, infrastructure, deployment, monitoring
- qa: testing, quality assurance, test automation

Respond with JSON:
{{
  "classifications": [
    {{"task_id": "TASK-1", "team": "frontend|backend|devops|qa", "reason": "brief reason"}}
  ],
  "team_summary": {{
    "frontend": 5,
    "backend": 8,
    "devops": 2,
    "qa": 3
  }},
  "summary": "brief summary"
}}
"""




def _extract_tasks_from_hierarchy(hierarchy: Optional[PlanningHierarchy]) -> List[Dict[str, str]]:
    """Extract all tasks from hierarchy for classification."""
    if not hierarchy:
        return []
    
    tasks = []
    for init in hierarchy.initiatives:
        for epic in init.epics:
            for story in epic.stories:
                for task in story.tasks:
                    tasks.append({
                        "id": task.id,
                        "title": task.title,
                        "description": task.description,
                        "current_team": task.assignee or "",
                    })
    return tasks


class TaskClassificationToolAgent:
    """
    Task Classification tool agent: classifies tasks into execution teams.
    
    Participates in Implementation phase only per the matrix.
    """

    def __init__(self, llm: Optional["LLMClient"] = None) -> None:
        self.llm = llm

    def plan(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Planning phase: Task Classification does not participate."""
        return ToolAgentPhaseOutput(summary="Task Classification planning not applicable (per matrix).")

    def execute(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Implementation phase: classify tasks into teams."""
        if not self.llm:
            return ToolAgentPhaseOutput(
                summary="Task Classification execute skipped (no LLM).",
                recommendations=["Classify tasks by team: frontend, backend, devops, qa"],
            )
        
        tasks = _extract_tasks_from_hierarchy(inp.hierarchy)
        if not tasks:
            return ToolAgentPhaseOutput(
                summary="Task Classification skipped (no tasks to classify).",
            )
        
        tasks_text = "\n".join(
            f"- {t['id']}: {t['title']} - {t['description'][:100]}"
            for t in tasks[:50]
        )
        
        prompt = TASK_CLASSIFICATION_PROMPT.format(tasks=tasks_text)
        data = parse_json_with_recovery(self.llm, prompt, agent_name="TaskClassification")
        
        classifications = data.get("classifications") or []
        team_summary = data.get("team_summary") or {}
        
        content_parts = ["# Task Classification\n\n"]
        
        if team_summary:
            content_parts.append("## Team Summary\n")
            for team, count in team_summary.items():
                content_parts.append(f"- **{team}:** {count} tasks\n")
            content_parts.append("\n")
        
        if classifications:
            content_parts.append("## Classifications\n")
            for c in classifications:
                if isinstance(c, dict):
                    task_id = c.get("task_id", "")
                    team = c.get("team", "")
                    reason = c.get("reason", "")
                    content_parts.append(f"- **{task_id}** → {team}: {reason}\n")
            content_parts.append("\n")
        
        files = {}
        if classifications:
            files["planning_v2/task_classification.md"] = "".join(content_parts)
        
        return ToolAgentPhaseOutput(
            summary=data.get("summary", f"Classified {len(classifications)} tasks."),
            files=files,
            metadata={
                "classifications": classifications,
                "team_summary": team_summary,
            },
        )

    def review(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Review phase: Task Classification does not participate."""
        return ToolAgentPhaseOutput(summary="Task Classification review not applicable (per matrix).")

    def problem_solve(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Problem-solving phase: Task Classification does not participate."""
        return ToolAgentPhaseOutput(summary="Task Classification problem_solve not applicable (per matrix).")

    def deliver(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Deliver phase: Task Classification does not participate."""
        return ToolAgentPhaseOutput(summary="Task Classification deliver not applicable (per matrix).")
