"""
Task Classification tool agent for planning-v2.

Participates in phases: Implementation only.
Classifies tasks into the right execution teams (frontend, backend, devops, qa).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from software_engineering_team.shared.deduplication import dedupe_by_key
from software_engineering_team.shared.models import PlanningHierarchy

from ...models import ToolAgentPhaseInput, ToolAgentPhaseOutput, planning_asset_path
from ...output_templates import looks_like_truncated_file_content, parse_fix_output, parse_task_classification_output
from ..json_utils import complete_text_with_continuation

if TYPE_CHECKING:
    from llm_service import LLMClient

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
    if merged["classifications"]:
        merged["classifications"] = dedupe_by_key(
            merged["classifications"],
            key_fn=lambda x: x.get("task_id", "") if isinstance(x, dict) else str(x),
        )
    return merged

TASK_CLASSIFICATION_PROMPT = """You are a Task Classification expert. Classify each task into one team: frontend, backend, devops, qa.

Respond using this EXACT format:

## CLASSIFICATIONS ##
TASK-1 | frontend | Login UI components
TASK-2 | backend | API endpoint
## END CLASSIFICATIONS ##

## SUMMARY ##
Brief summary.
## END SUMMARY ##

Tasks to classify:
---
{tasks}
---
"""

TASK_CLASSIFICATION_FIX_SINGLE_ISSUE_PROMPT = """You are a Task Classification expert. Fix this issue. Use this EXACT format:

## ROOT_CAUSE ##
Why this issue exists.
## END ROOT_CAUSE ##

## FIX_DESCRIPTION ##
What you are changing to fix it.
## END FIX_DESCRIPTION ##

## RESOLVED ##
true or false
## END RESOLVED ##

Output the complete updated file content; do not truncate. Include every section in full.

## FILE_UPDATES ##
### plan/planning_team/task_classification.md ###
Complete updated file content here.
### END FILE ###
## END FILE_UPDATES ##

ISSUE: --- {issue} ---
CURRENT ARTIFACT: --- {current_artifact} ---
SPEC: --- {spec_excerpt} ---
"""

TASK_CLASSIFICATION_FIX_ALL_ISSUES_PROMPT = """You are a Task Classification expert. Address ALL of the following issues in ONE coherent update. Use this EXACT format:

## ROOT_CAUSE ##
Brief combined root cause for the issues.
## END ROOT_CAUSE ##

## FIX_DESCRIPTION ##
What you are changing to address all issues.
## END FIX_DESCRIPTION ##

## RESOLVED ##
true or false
## END RESOLVED ##

Output the complete updated file content; do not truncate. Include every section in full.

## FILE_UPDATES ##
### plan/planning_team/task_classification.md ###
Complete updated file content here.
### END FILE ###
## END FILE_UPDATES ##

ISSUES TO FIX (address every one):
---
{issues_list}
---

CURRENT ARTIFACT: --- {current_artifact} ---
SPEC: --- {spec_excerpt} ---
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
        Writes to disk as fixes are applied; returns files_written so implementation phase does not overwrite.
        """
        fixes_applied: List[str] = []
        files_written: List[str] = []
        current_files: Dict[str, str] = dict(inp.current_files or {})
        
        classification_issues = [
            i for i in inp.review_issues
            if any(kw in i.lower() for kw in ["classification", "team assignment", "task team", "frontend", "backend", "devops", "qa"])
        ]
        
        if classification_issues and self.llm:
            logger.info(
                "TaskClassification: handling %d review issue(s) (will apply fixes in one update and write to disk).",
                len(classification_issues),
            )
            fix_inp = inp.model_copy(update={"current_files": current_files})
            result = self.fix_all_issues(classification_issues, fix_inp)
            if result.files:
                repo = Path(inp.repo_path or ".")
                for rel_path, content in result.files.items():
                    full_path = repo / rel_path
                    full_path.parent.mkdir(parents=True, exist_ok=True)
                    full_path.write_text(content, encoding="utf-8")
                    file_name = full_path.name
                    logger.info(
                        "TaskClassification: applied fix — writing to file: %s (%d chars)",
                        file_name,
                        len(content),
                    )
                    if rel_path not in files_written:
                        files_written.append(rel_path)
                    current_files[rel_path] = content
                fixes_applied.append(result.summary)
            logger.info(
                "TaskClassification: fixed %d review issue(s) in one update (all fixes written to planning artifacts).",
                len(classification_issues),
            )
        
        existing_doc = inp.current_files.get(planning_asset_path("task_classification.md")) if inp.current_files else None
        if existing_doc and not classification_issues:
            return ToolAgentPhaseOutput(
                summary="Task Classification artifacts unchanged (file exists, no review issues).",
                files={},
                recommendations=[],
                files_written=[],
            )
        if files_written:
            summary = "Task Classification artifacts updated."
            if fixes_applied:
                summary = f"Task Classification artifacts updated. Fixed {len(classification_issues)} review issue(s) in one update."
            return ToolAgentPhaseOutput(
                summary=summary,
                files={},
                recommendations=fixes_applied if fixes_applied else [],
                files_written=files_written,
            )
        
        if not self.llm:
            return ToolAgentPhaseOutput(
                summary="Task Classification execute skipped (no LLM).",
                recommendations=["Classify tasks by team: frontend, backend, devops, qa"],
                files_written=[],
            )
        
        tasks = _extract_tasks_from_hierarchy(inp.hierarchy)
        if not tasks:
            return ToolAgentPhaseOutput(
                summary="Task Classification skipped (no tasks to classify).",
                files_written=[],
            )
        
        tasks_text = "\n".join(
            f"- {t['id']}: {t['title']} - {t['description'][:100]}"
            for t in tasks[:50]
        )
        prompt = TASK_CLASSIFICATION_PROMPT.format(tasks=tasks_text)
        raw_text = complete_text_with_continuation(
            self.llm, prompt, agent_name="TaskClassification",
        )
        data = parse_task_classification_output(raw_text)
        classifications = data.get("classifications") or []
        team_summary: Dict[str, int] = {}
        for c in classifications:
            if isinstance(c, dict):
                team = c.get("team", "backend")
                team_summary[team] = team_summary.get(team, 0) + 1

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
            rel_path = planning_asset_path("task_classification.md")
            content = "".join(content_parts)
            repo = Path(inp.repo_path or ".")
            full_path = repo / rel_path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content, encoding="utf-8")
            files_written.append(rel_path)
        
        return ToolAgentPhaseOutput(
            summary=data.get("summary", f"Classified {len(classifications)} tasks."),
            files={},
            files_written=files_written,
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
            current_artifact = inp.current_files.get(planning_asset_path("task_classification.md"), "")
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
            raw_text = complete_text_with_continuation(
                self.llm, prompt, agent_name="TaskClassification_FixSingleIssue",
            )
            raw = parse_fix_output(raw_text)
            updated_content = raw.get("updated_content", "")
            fix_desc = raw.get("fix_description", "")
            resolved = raw.get("resolved", False)
            file_updates = raw.get("file_updates") or {}
            if not updated_content and file_updates:
                updated_content = next(iter(file_updates.values()), "")

            files: Dict[str, str] = {}
            if updated_content and isinstance(updated_content, str) and updated_content.strip():
                if looks_like_truncated_file_content(updated_content):
                    logger.warning(
                        "TaskClassification: fix output appears truncated (file content incomplete); skipping write to avoid incomplete artifact.",
                    )
                else:
                    files[planning_asset_path("task_classification.md")] = updated_content
                    logger.info("TaskClassification: fix applied (single-issue) — %s", fix_desc[:120])
            elif file_updates:
                for path, content in file_updates.items():
                    if content and isinstance(content, str) and content.strip() and not looks_like_truncated_file_content(content):
                        files[path] = content
                        logger.info("TaskClassification: fix applied (single-issue) — %s", fix_desc[:120])
                        break
                else:
                    if file_updates and any(isinstance(c, str) and c.strip() for c in file_updates.values()):
                        logger.warning(
                            "TaskClassification: fix output appears truncated (file content incomplete); skipping write to avoid incomplete artifact.",
                        )

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

    def fix_all_issues(
        self, issues: List[str], inp: ToolAgentPhaseInput
    ) -> ToolAgentPhaseOutput:
        """Fix all listed task classification issues in one LLM call."""
        if not issues:
            return ToolAgentPhaseOutput(
                summary="No task classification issues to fix.",
                resolved=True,
            )
        if not self.llm:
            return ToolAgentPhaseOutput(
                summary="Task Classification fix skipped (no LLM).",
                resolved=False,
            )

        current_artifact = ""
        if inp.current_files:
            current_artifact = inp.current_files.get(planning_asset_path("task_classification.md"), "")
            if not current_artifact:
                for path, content in inp.current_files.items():
                    if "task_classification" in path.lower() or "classification" in path.lower():
                        current_artifact = content
                        break

        issues_list = "\n".join(f"{i + 1}. {issue}" for i, issue in enumerate(issues))
        prompt = TASK_CLASSIFICATION_FIX_ALL_ISSUES_PROMPT.format(
            issues_list=issues_list,
            current_artifact=current_artifact[:6000] if current_artifact else "(no existing artifact)",
            spec_excerpt=(inp.spec_content or "")[:3000],
        )

        try:
            raw_text = complete_text_with_continuation(
                self.llm, prompt, agent_name="TaskClassification_FixAllIssues",
            )
            raw = parse_fix_output(raw_text)
            updated_content = raw.get("updated_content", "")
            fix_desc = raw.get("fix_description", "")
            resolved = raw.get("resolved", False)
            file_updates = raw.get("file_updates") or {}
            if not updated_content and file_updates:
                updated_content = next(iter(file_updates.values()), "")

            files: Dict[str, str] = {}
            if updated_content and isinstance(updated_content, str) and updated_content.strip():
                if looks_like_truncated_file_content(updated_content):
                    logger.warning(
                        "TaskClassification: fix_all_issues output appears truncated; skipping write.",
                    )
                else:
                    files[planning_asset_path("task_classification.md")] = updated_content
            elif file_updates:
                for path, content in file_updates.items():
                    if content and isinstance(content, str) and content.strip() and not looks_like_truncated_file_content(content):
                        files[path] = content
                        break
                else:
                    if file_updates and any(isinstance(c, str) and c.strip() for c in file_updates.values()):
                        logger.warning(
                            "TaskClassification: fix_all_issues output appears truncated; skipping write.",
                        )

            summary = fix_desc or f"Addressed {len(issues)} issue(s) in one update."
            if len(issues) > 1:
                summary = f"Addressed {len(issues)} issues in one update. {summary[:200]}"
            return ToolAgentPhaseOutput(
                summary=summary,
                files=files,
                resolved=resolved or bool(files),
                metadata={"root_cause": raw.get("root_cause", "")},
            )
        except Exception as e:
            logger.warning("TaskClassification fix_all_issues failed: %s", e)
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
