"""
Task Classification tool agent for planning-v2.

Participates in phases: Implementation only.
Classifies tasks into the right execution teams (frontend, backend, devops, qa).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from shared.models import PlanningHierarchy

from ...models import ToolAgentPhaseInput, ToolAgentPhaseOutput
from ..json_utils import parse_json_with_recovery, complete_with_continuation

if TYPE_CHECKING:
    from shared.llm import LLMClient

logger = logging.getLogger(__name__)


def _decompose_tasks_into_batches(
    tasks_json: str, batch_size: int = 10
) -> List[str]:
    """Decompose task list into smaller batches."""
    try:
        tasks = json.loads(tasks_json)
        if not isinstance(tasks, list):
            return [tasks_json]
    except json.JSONDecodeError:
        return [tasks_json]

    if len(tasks) <= batch_size:
        return [tasks_json]

    batches = []
    for i in range(0, len(tasks), batch_size):
        batch = tasks[i : i + batch_size]
        batches.append(json.dumps(batch, indent=2))
    return batches


def _merge_task_classification_results(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge task classification results from multiple batches."""
    merged: Dict[str, Any] = {
        "classifications": [],
        "team_summary": {"frontend": 0, "backend": 0, "devops": 0, "qa": 0},
        "summary": "",
    }
    summaries = []

    for r in results:
        if isinstance(r.get("classifications"), list):
            merged["classifications"].extend(r["classifications"])
        if isinstance(r.get("team_summary"), dict):
            for team, count in r["team_summary"].items():
                if team in merged["team_summary"] and isinstance(count, (int, float)):
                    merged["team_summary"][team] += int(count)
        if r.get("summary"):
            summaries.append(str(r["summary"]))

    merged["summary"] = (
        f"Classified {len(merged['classifications'])} tasks. " + " ".join(summaries[:2])
    )
    return merged

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

TASK_CLASSIFICATION_CHUNK_PROMPT = """You are a Task Classification expert. Classify these tasks into teams:

TASKS:
---
{chunk_content}
---

Teams: frontend, backend, devops, qa

Respond with concise JSON:
{{
  "classifications": [
    {{"task_id": "TASK-1", "team": "frontend|backend|devops|qa", "reason": "brief reason"}}
  ],
  "team_summary": {{"frontend": 0, "backend": 0, "devops": 0, "qa": 0}},
  "summary": "brief summary"
}}
"""

TASK_CLASSIFICATION_FIX_SINGLE_ISSUE_PROMPT = """You are a Task Classification expert. Fix this specific issue in the task classification artifacts.

ISSUE TO FIX:
---
{issue}
---

CURRENT TASK CLASSIFICATION ARTIFACT:
---
{current_artifact}
---

SPECIFICATION CONTEXT:
---
{spec_excerpt}
---

Analyze and fix this issue. If the issue relates to task team assignments or classification, provide the complete updated file content.

Respond with JSON:
{{
  "root_cause": "why this issue exists",
  "fix_description": "what you are changing to fix it",
  "resolved": true or false,
  "updated_content": "the complete updated file content (or empty string if no change needed)"
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
        """Implementation phase: classify tasks into teams or update existing classifications.
        
        If review_issues are provided, this agent handles fixes first.
        Only regenerates the document if it doesn't already exist.
        """
        all_files: Dict[str, str] = {}
        fixes_applied: List[str] = []
        
        classification_issues = [
            i for i in inp.review_issues
            if any(kw in i.lower() for kw in ["classification", "team assignment", "task team", "frontend", "backend", "devops", "qa"])
        ]
        
        if classification_issues and self.llm:
            logger.info("TaskClassification: handling %d review issues", len(classification_issues))
            for issue in classification_issues:
                result = self.fix_single_issue(issue, inp)
                if result.files:
                    all_files.update(result.files)
                    fixes_applied.append(result.summary)
            logger.info("TaskClassification: fixed %d/%d issues", len(fixes_applied), len(classification_issues))
        
        existing_doc = inp.current_files.get("plan/task_classification.md") if inp.current_files else None
        if existing_doc or all_files.get("plan/task_classification.md"):
            summary = "Task Classification artifacts updated."
            if fixes_applied:
                summary = f"Task Classification artifacts updated. Fixed {len(fixes_applied)} review issues."
            return ToolAgentPhaseOutput(
                summary=summary,
                files=all_files,
                recommendations=fixes_applied if fixes_applied else [],
            )
        
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
        tasks_json = json.dumps([{"id": t["id"], "title": t["title"], "description": t["description"][:100]} for t in tasks[:50]])
        
        prompt = TASK_CLASSIFICATION_PROMPT.format(tasks=tasks_text)
        data = parse_json_with_recovery(
            self.llm,
            prompt,
            agent_name="TaskClassification",
            decompose_fn=_decompose_tasks_into_batches,
            merge_fn=_merge_task_classification_results,
            original_content=tasks_json,
            chunk_prompt_template=TASK_CLASSIFICATION_CHUNK_PROMPT,
        )
        
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
        
        if classifications:
            all_files["plan/task_classification.md"] = "".join(content_parts)
        
        return ToolAgentPhaseOutput(
            summary=data.get("summary", f"Classified {len(classifications)} tasks."),
            files=all_files,
            metadata={
                "classifications": classifications,
                "team_summary": team_summary,
            },
        )

    def fix_single_issue(self, issue: str, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Fix a single task classification issue.

        Args:
            issue: The issue description to fix.
            inp: Tool agent phase input with context.

        Returns:
            ToolAgentPhaseOutput with updated files if fix was applied.
        """
        if not self.llm:
            return ToolAgentPhaseOutput(
                summary="Task Classification fix skipped (no LLM).",
                resolved=False,
            )

        current_artifact = ""
        if inp.current_files:
            current_artifact = inp.current_files.get("plan/task_classification.md", "")
            if not current_artifact:
                for path, content in inp.current_files.items():
                    if "task_classification" in path.lower() or "classification" in path.lower():
                        current_artifact = content
                        break

        prompt = TASK_CLASSIFICATION_FIX_SINGLE_ISSUE_PROMPT.format(
            issue=issue,
            current_artifact=current_artifact[:6000] if current_artifact else "(no existing artifact)",
            spec_excerpt=(inp.spec_content or "")[:3000],
        )

        try:
            raw = complete_with_continuation(
                llm=self.llm,
                prompt=prompt,
                mode="json",
                agent_name="TaskClassification_FixSingleIssue",
            )

            if not isinstance(raw, dict):
                return ToolAgentPhaseOutput(
                    summary="Fix failed: invalid response format",
                    resolved=False,
                )

            updated_content = raw.get("updated_content", "")
            fix_desc = raw.get("fix_description", "")
            resolved = raw.get("resolved", False)

            files: Dict[str, str] = {}
            if updated_content and isinstance(updated_content, str) and updated_content.strip():
                files["plan/task_classification.md"] = updated_content
                logger.info("TaskClassification: fix applied — %s", fix_desc[:60])

            return ToolAgentPhaseOutput(
                summary=fix_desc or f"Task classification issue addressed: {issue[:50]}",
                files=files,
                resolved=resolved or bool(files),
                metadata={"root_cause": raw.get("root_cause", "")},
            )

        except Exception as e:
            logger.warning("TaskClassification fix_single_issue failed: %s", e)
            return ToolAgentPhaseOutput(
                summary=f"Fix failed: {str(e)[:50]}",
                resolved=False,
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
