"""
Task Dependency tool agent for planning-v2.

Participates in phases: Review only.
Analyzes dependencies between tasks to ensure proper execution order.
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

TASK_DEPENDENCY_REVIEW_PROMPT = """You are a Task Dependency Analyzer. Review these tasks and identify dependencies.

Tasks:
---
{tasks}
---

Analyze:
1. Which tasks block others (must complete before another can start)
2. Circular dependency risks
3. Critical path (longest chain of dependent tasks)
4. Parallelization opportunities

Respond with JSON:
{{
  "dependencies": [
    {{"from_task": "TASK-1", "to_task": "TASK-2", "type": "blocks|requires|enables"}}
  ],
  "circular_risks": ["description of any circular dependency risks"],
  "critical_path": ["TASK-1", "TASK-2", "TASK-3"],
  "parallelizable": [["TASK-4", "TASK-5"], ["TASK-6", "TASK-7"]],
  "issues": ["any dependency issues found"],
  "summary": "brief summary"
}}
"""




def _extract_tasks_from_hierarchy(hierarchy: Optional[PlanningHierarchy]) -> List[Dict[str, str]]:
    """Extract all tasks from hierarchy for dependency analysis."""
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
                        "team": task.assignee or "",
                    })
    return tasks


class TaskDependencyToolAgent:
    """
    Task Dependency tool agent: analyzes dependencies between tasks.
    
    Participates in Review phase only per the matrix.
    """

    def __init__(self, llm: Optional["LLMClient"] = None) -> None:
        self.llm = llm

    def plan(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Planning phase: Task Dependency does not participate."""
        return ToolAgentPhaseOutput(summary="Task Dependency planning not applicable (per matrix).")

    def execute(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Implementation phase: Task Dependency does not participate."""
        return ToolAgentPhaseOutput(summary="Task Dependency execute not applicable (per matrix).")

    def review(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Review phase: analyze task dependencies."""
        if not self.llm:
            return ToolAgentPhaseOutput(
                summary="Task Dependency review skipped (no LLM).",
                recommendations=["Analyze task dependencies manually"],
            )
        
        tasks = _extract_tasks_from_hierarchy(inp.hierarchy)
        
        if not tasks:
            task_artifacts = "\n".join(
                f"--- {path} ---\n{content[:500]}"
                for path, content in list(inp.current_files.items())[:5]
                if "task" in path.lower() or "story" in path.lower()
            )[:4000]
            if not task_artifacts.strip():
                return ToolAgentPhaseOutput(
                    summary="Task Dependency review skipped (no tasks).",
                    issues=[],
                )
            tasks_text = task_artifacts
        else:
            tasks_text = "\n".join(
                f"- {t['id']}: [{t['team']}] {t['title']} - {t['description'][:80]}"
                for t in tasks[:50]
            )
        
        prompt = TASK_DEPENDENCY_REVIEW_PROMPT.format(tasks=tasks_text)
        data = parse_json_with_recovery(self.llm, prompt, agent_name="TaskDependency")
        
        dependencies = data.get("dependencies") or []
        circular_risks = data.get("circular_risks") or []
        critical_path = data.get("critical_path") or []
        parallelizable = data.get("parallelizable") or []
        issues = data.get("issues") or []
        
        if not isinstance(issues, list):
            issues = [str(issues)] if issues else []
        
        content_parts = ["# Task Dependency Analysis\n\n"]
        
        if critical_path:
            content_parts.append("## Critical Path\n")
            content_parts.append(" → ".join(critical_path))
            content_parts.append("\n\n")
        
        if dependencies:
            content_parts.append("## Dependencies\n")
            for dep in dependencies:
                if isinstance(dep, dict):
                    from_task = dep.get("from_task", "")
                    to_task = dep.get("to_task", "")
                    dep_type = dep.get("type", "blocks")
                    content_parts.append(f"- {from_task} --[{dep_type}]--> {to_task}\n")
            content_parts.append("\n")
        
        if parallelizable:
            content_parts.append("## Parallelization Opportunities\n")
            for group in parallelizable:
                if isinstance(group, list):
                    content_parts.append(f"- {', '.join(group)}\n")
            content_parts.append("\n")
        
        if circular_risks:
            content_parts.append("## Circular Dependency Risks\n")
            for risk in circular_risks:
                content_parts.append(f"- ⚠️ {risk}\n")
            content_parts.append("\n")
        
        files = {}
        if dependencies or critical_path:
            files["planning_v2/task_dependencies.md"] = "".join(content_parts)
        
        all_issues = issues + circular_risks
        
        return ToolAgentPhaseOutput(
            summary=data.get("summary", f"Analyzed {len(dependencies)} dependencies."),
            issues=all_issues,
            files=files,
            metadata={
                "dependencies": dependencies,
                "critical_path": critical_path,
                "parallelizable": parallelizable,
            },
        )

    def problem_solve(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Problem-solving phase: Task Dependency does not participate."""
        return ToolAgentPhaseOutput(summary="Task Dependency problem_solve not applicable (per matrix).")

    def deliver(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Deliver phase: Task Dependency does not participate."""
        return ToolAgentPhaseOutput(summary="Task Dependency deliver not applicable (per matrix).")
