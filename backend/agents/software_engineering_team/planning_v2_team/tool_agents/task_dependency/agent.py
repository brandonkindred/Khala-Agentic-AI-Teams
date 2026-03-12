"""
Task Dependency tool agent for planning-v2.

Participates in phases: Review only.
Analyzes dependencies between tasks to ensure proper execution order.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from software_engineering_team.shared.models import PlanningHierarchy

from ...models import ToolAgentPhaseInput, ToolAgentPhaseOutput, planning_asset_path
from ...output_templates import parse_review_output
from ..json_utils import complete_text_with_continuation

if TYPE_CHECKING:
    from llm_service import LLMClient

logger = logging.getLogger(__name__)


def _decompose_tasks_into_groups(tasks_json: str, group_size: int = 15) -> List[str]:
    """Decompose task list into smaller groups for dependency analysis."""
    try:
        tasks = json.loads(tasks_json)
        if not isinstance(tasks, list):
            return [tasks_json]
    except json.JSONDecodeError:
        return [tasks_json]

    if len(tasks) <= group_size:
        return [tasks_json]

    groups = []
    for i in range(0, len(tasks), group_size):
        group = tasks[i : i + group_size]
        groups.append(json.dumps(group, indent=2))
    return groups


def _merge_task_dependency_results(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge task dependency results from multiple groups."""
    merged: Dict[str, Any] = {
        "dependencies": [],
        "circular_risks": [],
        "critical_path": [],
        "parallelizable": [],
        "issues": [],
        "summary": "",
    }
    summaries = []

    for r in results:
        if isinstance(r.get("dependencies"), list):
            merged["dependencies"].extend(r["dependencies"])
        if isinstance(r.get("circular_risks"), list):
            merged["circular_risks"].extend(r["circular_risks"])
        if isinstance(r.get("critical_path"), list) and len(r["critical_path"]) > len(
            merged["critical_path"]
        ):
            merged["critical_path"] = r["critical_path"]
        if isinstance(r.get("parallelizable"), list):
            merged["parallelizable"].extend(r["parallelizable"])
        if isinstance(r.get("issues"), list):
            merged["issues"].extend(r["issues"])
        if r.get("summary"):
            summaries.append(str(r["summary"]))

    merged["summary"] = (
        f"Analyzed {len(merged['dependencies'])} dependencies. " + " ".join(summaries[:2])
    )
    if merged["dependencies"]:
        merged["dependencies"] = dedupe_by_key(
            merged["dependencies"],
            key_fn=lambda x: f"{x.get('from_task','')}|{x.get('to_task','')}" if isinstance(x, dict) else str(x),
        )
    if all(isinstance(x, str) for x in merged["circular_risks"]):
        merged["circular_risks"] = dedupe_strings(merged["circular_risks"])
    if merged["parallelizable"]:
        merged["parallelizable"] = dedupe_by_key(
            merged["parallelizable"],
            key_fn=lambda x: "|".join(sorted(x)) if isinstance(x, list) else str(x),
        )
    if all(isinstance(x, str) for x in merged["issues"]):
        merged["issues"] = dedupe_strings(merged["issues"])[:DEFAULT_MAX_ISSUES]
    return merged

TASK_DEPENDENCY_REVIEW_PROMPT = """You are an expert Task Dependency Analyzer. Review these tasks and identify dependency issues, circular risks, and a brief summary.

Tasks:
---
{tasks}
---

Respond using this EXACT format:

## PASSED ##
true or false
## END PASSED ##

## ISSUES ##
- Dependency issue 1 (e.g. TASK-1 blocks TASK-2)
- Circular risk or other issue
## END ISSUES ##

## RECOMMENDATIONS ##
- Recommendation 1
## END RECOMMENDATIONS ##

## SUMMARY ##
Brief summary.
## END SUMMARY ##
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
        
        tasks_json = ""
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
            tasks_json = json.dumps([{"id": t["id"], "team": t["team"], "title": t["title"], "description": t["description"][:80]} for t in tasks[:50]])
        
        prompt = TASK_DEPENDENCY_REVIEW_PROMPT.format(tasks=tasks_text)
        raw_text = complete_text_with_continuation(
            self.llm, prompt, agent_name="TaskDependency",
        )
        data = parse_review_output(raw_text)
        issues = data.get("issues") or []
        recommendations = data.get("recommendations") or []
        summary = data.get("summary", "Task dependency review complete.")
        dependencies: List[Dict[str, Any]] = []
        circular_risks: List[str] = []
        critical_path: List[str] = []
        parallelizable: List[List[str]] = []
        if not isinstance(issues, list):
            issues = [str(issues)] if issues else []

        content_parts = ["# Task Dependency Analysis\n\n"]
        content_parts.append(f"## Summary\n{summary}\n\n")
        if issues:
            content_parts.append("## Issues\n")
            for issue in issues:
                content_parts.append(f"- {issue}\n")
            content_parts.append("\n")
        if recommendations:
            content_parts.append("## Recommendations\n")
            for rec in recommendations:
                content_parts.append(f"- {rec}\n")
            content_parts.append("\n")

        files: Dict[str, str] = {}
        if issues or recommendations or summary:
            files[planning_asset_path("task_dependencies.md")] = "".join(content_parts)

        return ToolAgentPhaseOutput(
            summary=summary,
            issues=issues,
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
