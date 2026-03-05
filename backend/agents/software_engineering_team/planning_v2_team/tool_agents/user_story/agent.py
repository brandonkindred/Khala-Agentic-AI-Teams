"""
User Story tool agent for planning-v2.

Participates in phases: Planning, Implementation, Review, Problem Solving, Deliver.
Produces the hierarchical output: Initiative -> Epic -> Story -> Task.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from software_engineering_team.shared.models import Initiative, Epic, StoryPlan, TaskPlan, PlanningHierarchy

from ...models import ToolAgentPhaseInput, ToolAgentPhaseOutput, planning_asset_path
from ...output_templates import parse_fix_output, parse_review_output
from ..json_utils import complete_text_with_continuation
from software_engineering_team.shared.deduplication import dedupe_by_key

if TYPE_CHECKING:
    from software_engineering_team.shared.llm import LLMClient

logger = logging.getLogger(__name__)


def _parse_hierarchy_text_to_data(text: str) -> Dict[str, Any]:
    """Parse line-based hierarchy format into dict for _build_hierarchy_from_data.

    Format (one line per node; pipe-separated):
    INIT | id | title | description
    EPIC | id | title | description
    STORY | id | title | description
    TASK | id | title | assignee | complexity_points
    Hierarchy: EPIC under last INIT, STORY under last EPIC, TASK under last STORY.
    """
    initiatives: List[Dict[str, Any]] = []
    current_init: Optional[Dict[str, Any]] = None
    current_epic: Optional[Dict[str, Any]] = None
    current_story: Optional[Dict[str, Any]] = None
    summary = ""

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2:
            continue
        kind = parts[0].upper()
        if kind == "INIT" and len(parts) >= 3:
            current_init = {"id": parts[1], "title": parts[2], "description": parts[3] if len(parts) > 3 else "", "epics": []}
            initiatives.append(current_init)
            current_epic = None
            current_story = None
        elif kind == "EPIC" and len(parts) >= 3 and current_init:
            current_epic = {"id": parts[1], "title": parts[2], "description": parts[3] if len(parts) > 3 else "", "acceptance_criteria": [], "stories": []}
            current_init["epics"].append(current_epic)
            current_story = None
        elif kind == "STORY" and len(parts) >= 3 and current_epic:
            current_story = {"id": parts[1], "title": parts[2], "description": parts[3] if len(parts) > 3 else "", "acceptance_criteria": [], "tasks": []}
            current_epic["stories"].append(current_story)
        elif kind == "TASK" and len(parts) >= 4 and current_story:
            assignee = parts[3] if len(parts) > 3 else "backend"
            points = 2
            if len(parts) > 4 and str(parts[4]).strip().isdigit():
                points = min(13, max(1, int(parts[4])))
            current_story["tasks"].append({
                "id": parts[1],
                "title": parts[2],
                "description": "",
                "assigned_team": assignee,
                "acceptance_criteria": [],
                "complexity_points": points,
            })

    summary_section = text.find("## SUMMARY ##")
    if summary_section >= 0:
        end = text.find("## END SUMMARY ##", summary_section)
        if end > summary_section:
            summary = text[summary_section + len("## SUMMARY ##"):end].strip().split("\n")[0].strip()[:500]

    return {"initiatives": initiatives, "summary": summary or "User story hierarchy created."}


def _merge_user_story_results(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge user story results from multiple chunks with semantic deduplication by initiative name."""
    merged: Dict[str, Any] = {
        "initiatives": [],
        "summary": "",
    }
    summaries = []

    for r in results:
        if isinstance(r.get("initiatives"), list):
            merged["initiatives"].extend(r["initiatives"])
        if r.get("summary"):
            summaries.append(str(r["summary"]))

    merged["summary"] = f"Merged {len(results)} sections. " + " ".join(summaries[:2])
    if merged["initiatives"]:
        merged["initiatives"] = dedupe_by_key(
            merged["initiatives"],
            key_fn=lambda x: x.get("name", x.get("title", "")) if isinstance(x, dict) else str(x),
        )
    return merged


USER_STORY_PLANNING_PROMPT = """You are a Product Planning expert. Create a hierarchical plan: Initiative -> Epic -> Story -> Task.

Output EXACTLY one line per node using this format (pipe-separated). No JSON.

INIT | id | title | description
EPIC | id | title | description
STORY | id | title | description
TASK | id | title | assignee | complexity_points

Rules:
- Each task should be Fibonacci 2-3 points (1-2 days, focused). Use complexity_points 2 or 3.
- assignee is one of: frontend, backend, devops, qa
- List INIT first, then EPIC under it, then STORY under EPIC, then TASK under STORY. Order matters.

At the end add:
## SUMMARY ##
Brief planning summary.
## END SUMMARY ##

Specification:
---
{spec_content}
---

Plan Summary: {plan_summary}
"""

USER_STORY_REVIEW_PROMPT = """You are a Product Planning expert. Review these user stories and tasks for completeness, team assignments, task granularity (Fibonacci 2-3 points), and flag issues.

Artifacts:
---
{artifacts}
---

Respond using this EXACT format:

## PASSED ##
true or false
## END PASSED ##

## ISSUES ##
- Issue 1
- Issue 2
## END ISSUES ##

## RECOMMENDATIONS ##
- Improvement 1
## END RECOMMENDATIONS ##

## SUMMARY ##
Brief summary.
## END SUMMARY ##
"""

USER_STORY_FIX_SINGLE_ISSUE_PROMPT = """You are a Product Planning expert. Fix this specific issue in the user story artifacts.

ISSUE TO FIX:
---
{issue}
---

CURRENT USER STORY ARTIFACT:
---
{current_artifact}
---

SPECIFICATION CONTEXT:
---
{spec_excerpt}
---

Analyze and fix this issue. If the issue requires updating the artifact, provide the complete updated file content.

Respond using this EXACT format:

## ROOT_CAUSE ##
Why this issue exists.
## END ROOT_CAUSE ##

## FIX_DESCRIPTION ##
What you are changing to fix it.
## END FIX_DESCRIPTION ##

## RESOLVED ##
true or false
## END RESOLVED ##

## FILE_UPDATES ##
### plan/planning_team/user_stories.md ###
Complete updated file content here.
### END FILE ###
## END FILE_UPDATES ##
"""


def _build_hierarchy_from_data(data: Dict[str, Any]) -> Optional[PlanningHierarchy]:
    """Build PlanningHierarchy from LLM response data."""
    initiatives_data = data.get("initiatives") or []
    if not initiatives_data:
        return None
    
    initiatives: List[Initiative] = []
    for init_data in initiatives_data:
        if not isinstance(init_data, dict):
            continue
        
        epics: List[Epic] = []
        for epic_data in init_data.get("epics") or []:
            if not isinstance(epic_data, dict):
                continue
            
            stories: List[StoryPlan] = []
            for story_data in epic_data.get("stories") or []:
                if not isinstance(story_data, dict):
                    continue
                
                tasks: List[TaskPlan] = []
                for task_data in story_data.get("tasks") or []:
                    if not isinstance(task_data, dict):
                        continue
                    
                    ac = task_data.get("acceptance_criteria") or []
                    if not isinstance(ac, list):
                        ac = [str(ac)] if ac else []
                    
                    assigned_team = task_data.get("assigned_team", "") or task_data.get("assignee", "backend")
                    
                    tasks.append(TaskPlan(
                        id=task_data.get("id", ""),
                        title=task_data.get("title", ""),
                        description=task_data.get("description", ""),
                        acceptance_criteria=ac,
                        assignee=assigned_team,
                        example=task_data.get("example"),
                    ))
                
                story_ac = story_data.get("acceptance_criteria") or []
                if not isinstance(story_ac, list):
                    story_ac = [str(story_ac)] if story_ac else []
                
                stories.append(StoryPlan(
                    id=story_data.get("id", ""),
                    title=story_data.get("title", ""),
                    description=story_data.get("description", ""),
                    acceptance_criteria=story_ac,
                    example=story_data.get("example", ""),
                    tasks=tasks,
                ))
            
            epic_ac = epic_data.get("acceptance_criteria") or []
            if not isinstance(epic_ac, list):
                epic_ac = [str(epic_ac)] if epic_ac else []
            
            epics.append(Epic(
                id=epic_data.get("id", ""),
                title=epic_data.get("title", ""),
                description=epic_data.get("description", ""),
                acceptance_criteria=epic_ac,
                stories=stories,
            ))
        
        initiatives.append(Initiative(
            id=init_data.get("id", ""),
            title=init_data.get("title", ""),
            description=init_data.get("description", ""),
            epics=epics,
        ))
    
    return PlanningHierarchy(initiatives=initiatives)


def _hierarchy_to_markdown(hierarchy: PlanningHierarchy) -> str:
    """Convert PlanningHierarchy to markdown document."""
    parts = ["# Planning Hierarchy\n\n"]
    
    for init in hierarchy.initiatives:
        parts.append(f"## Initiative: {init.title}\n")
        parts.append(f"**ID:** {init.id}\n")
        parts.append(f"**Description:** {init.description}\n\n")
        
        for epic in init.epics:
            parts.append(f"### Epic: {epic.title}\n")
            parts.append(f"**ID:** {epic.id}\n")
            parts.append(f"**Description:** {epic.description}\n")
            if epic.acceptance_criteria:
                parts.append("**Acceptance Criteria:**\n")
                for ac in epic.acceptance_criteria:
                    parts.append(f"- {ac}\n")
            parts.append("\n")
            
            for story in epic.stories:
                parts.append(f"#### Story: {story.title}\n")
                parts.append(f"**ID:** {story.id}\n")
                parts.append(f"**Description:** {story.description}\n")
                if story.acceptance_criteria:
                    parts.append("**Acceptance Criteria:**\n")
                    for ac in story.acceptance_criteria:
                        parts.append(f"- {ac}\n")
                if story.example:
                    parts.append(f"**Example:** {story.example}\n")
                parts.append("\n")
                
                if story.tasks:
                    parts.append("**Tasks:**\n\n")
                    for task in story.tasks:
                        parts.append(f"##### Task: {task.title}\n")
                        parts.append(f"- **ID:** {task.id}\n")
                        parts.append(f"- **Description:** {task.description}\n")
                        parts.append(f"- **Assigned Team:** {task.assignee}\n")
                        if task.acceptance_criteria:
                            parts.append("- **Acceptance Criteria:**\n")
                            for ac in task.acceptance_criteria:
                                parts.append(f"  - {ac}\n")
                        if task.example:
                            parts.append(f"- **Example:** {task.example}\n")
                        parts.append("\n")
    
    return "".join(parts)


class UserStoryToolAgent:
    """
    User Story tool agent: creates Initiative -> Epic -> Story -> Task hierarchy.
    
    Participates in Planning, Implementation, Review, Problem Solving, Deliver phases.
    """

    def __init__(self, llm: Optional["LLMClient"] = None) -> None:
        self.llm = llm

    def plan(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Planning phase: create user story hierarchy."""
        if not self.llm:
            return ToolAgentPhaseOutput(
                summary="User Story planning skipped (no LLM).",
                recommendations=["Create initiative/epic/story/task hierarchy"],
            )
        
        plan_summary = ""
        if inp.spec_review_result:
            plan_summary = getattr(inp.spec_review_result, "plan_summary", "") or ""

        spec_content = inp.spec_content or ""
        prompt = USER_STORY_PLANNING_PROMPT.format(
            spec_content=spec_content[:10000],
            plan_summary=plan_summary[:3000],
        )
        raw_text = complete_text_with_continuation(
            self.llm, prompt, agent_name="UserStory",
        )
        data = _parse_hierarchy_text_to_data(raw_text)
        hierarchy = _build_hierarchy_from_data(data)
        
        init_count = len(hierarchy.initiatives) if hierarchy else 0
        epic_count = sum(len(i.epics) for i in hierarchy.initiatives) if hierarchy else 0
        story_count = sum(len(e.stories) for i in hierarchy.initiatives for e in i.epics) if hierarchy else 0
        task_count = sum(len(s.tasks) for i in hierarchy.initiatives for e in i.epics for s in e.stories) if hierarchy else 0
        
        return ToolAgentPhaseOutput(
            summary=data.get("summary", f"Created {init_count} initiatives, {epic_count} epics, {story_count} stories, {task_count} tasks."),
            hierarchy=hierarchy,
            recommendations=[
                f"Review {init_count} initiatives for completeness",
                f"Verify {task_count} tasks have clear assignments",
            ],
        )

    def execute(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Implementation phase: generate user story artifacts and fix review issues.
        
        Applies fixes by writing the document to disk after each fix (update-in-place).
        Returns files_written so the implementation phase does not overwrite.
        """
        hierarchy = inp.hierarchy
        fixes_applied: List[str] = []
        files_written: List[str] = []
        current_files: Dict[str, str] = dict(inp.current_files or {})
        
        story_issues = [
            i for i in inp.review_issues
            if any(kw in i.lower() for kw in ["story", "task", "epic", "user", "acceptance", "criteria"])
        ]
        
        if story_issues and self.llm:
            logger.info(
                "UserStory: handling %d review issue(s) (will apply fixes and write updated artifacts to disk).",
                len(story_issues),
            )
            fix_inp = inp.model_copy(update={"current_files": current_files})
            for issue in story_issues:
                result = self.fix_single_issue(issue, fix_inp)
                if result.files:
                    repo = Path(inp.repo_path or ".")
                    for rel_path, content in result.files.items():
                        full_path = repo / rel_path
                        full_path.parent.mkdir(parents=True, exist_ok=True)
                        full_path.write_text(content, encoding="utf-8")
                        file_name = full_path.name
                        logger.info(
                            "UserStory: applied fix — writing to file: %s; full contents:\n%s",
                            file_name,
                            content,
                        )
                        if rel_path not in files_written:
                            files_written.append(rel_path)
                        current_files[rel_path] = content
                    fix_inp = inp.model_copy(update={"current_files": current_files})
                    fixes_applied.append(result.summary)
                if result.hierarchy:
                    hierarchy = result.hierarchy
            logger.info(
                "UserStory: fixed %d out of %d review issue(s) (all fixes written to planning artifacts).",
                len(fixes_applied),
                len(story_issues),
            )
        
        if not hierarchy:
            return ToolAgentPhaseOutput(
                summary="User Story execute skipped (no hierarchy).",
                recommendations=fixes_applied if fixes_applied else [],
                files_written=files_written,
            )
        
        existing_user_stories = (inp.current_files or {}).get(planning_asset_path("user_stories.md"))
        if existing_user_stories and not story_issues:
            return ToolAgentPhaseOutput(
                summary="User story artifacts unchanged (file exists, no review issues).",
                files={},
                hierarchy=hierarchy,
                recommendations=fixes_applied if fixes_applied else [],
                files_written=[],
            )
        
        if not files_written:
            content = _hierarchy_to_markdown(hierarchy)
            repo = Path(inp.repo_path or ".")
            rel_path = planning_asset_path("user_stories.md")
            full_path = repo / rel_path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content, encoding="utf-8")
            files_written = [rel_path]
        
        summary = "User story artifacts generated."
        if fixes_applied:
            summary = f"User story artifacts generated. Fixed {len(fixes_applied)} review issues."
        
        return ToolAgentPhaseOutput(
            summary=summary,
            files={},
            hierarchy=hierarchy,
            recommendations=fixes_applied if fixes_applied else [],
            files_written=files_written,
        )

    def review(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Review phase: check user story completeness."""
        if not self.llm:
            return ToolAgentPhaseOutput(summary="User Story review skipped (no LLM).")
        
        artifacts = "\n".join(
            f"--- {path} ---\n{content}"
            for path, content in list(inp.current_files.items())[:10]
            if "user_stor" in path.lower() or "planning" in path.lower()
        )[:8000]
        
        if not artifacts.strip():
            return ToolAgentPhaseOutput(
                summary="User Story review skipped (no artifacts).",
                issues=[],
            )
        
        prompt = USER_STORY_REVIEW_PROMPT.format(artifacts=artifacts)
        raw_text = complete_text_with_continuation(
            self.llm, prompt, agent_name="UserStory_Review",
        )
        data = parse_review_output(raw_text)
        
        issues = data.get("issues") or []
        if not isinstance(issues, list):
            issues = [str(issues)] if issues else []
        
        recommendations = data.get("recommendations") or []
        if not isinstance(recommendations, list):
            recommendations = [str(recommendations)] if recommendations else []
        
        return ToolAgentPhaseOutput(
            summary=data.get("summary", "User story review complete."),
            issues=issues,
            recommendations=recommendations,
        )

    def problem_solve(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Problem-solving phase: address user story issues."""
        if not self.llm:
            return ToolAgentPhaseOutput(summary="User Story problem_solve skipped (no LLM).")

        story_issues = [i for i in inp.review_issues if "story" in i.lower() or "task" in i.lower() or "epic" in i.lower()]
        if not story_issues:
            return ToolAgentPhaseOutput(summary="No user story issues to resolve.")

        all_files: Dict[str, str] = {}
        fixes_applied: List[str] = []

        for issue in story_issues:
            result = self.fix_single_issue(issue, inp)
            if result.files:
                all_files.update(result.files)
                fixes_applied.append(result.summary)

        return ToolAgentPhaseOutput(
            summary=f"User Story: fixed {len(fixes_applied)}/{len(story_issues)} issue(s).",
            recommendations=fixes_applied,
            files=all_files,
            resolved=len(fixes_applied) == len(story_issues),
        )

    def fix_single_issue(self, issue: str, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Fix a single user story issue.

        Args:
            issue: The issue description to fix.
            inp: Tool agent phase input with context.

        Returns:
            ToolAgentPhaseOutput with updated files if fix was applied.
        """
        if not self.llm:
            return ToolAgentPhaseOutput(
                summary="User Story fix skipped (no LLM).",
                resolved=False,
            )

        current_artifact = inp.current_files.get(planning_asset_path("user_stories.md"), "")
        if not current_artifact:
            for path, content in inp.current_files.items():
                if "user_stor" in path.lower() or "planning" in path.lower():
                    current_artifact = content
                    break

        prompt = USER_STORY_FIX_SINGLE_ISSUE_PROMPT.format(
            issue=issue,
            current_artifact=current_artifact[:6000] if current_artifact else "(no existing artifact)",
            spec_excerpt=(inp.spec_content or "")[:3000],
        )

        try:
            raw_text = complete_text_with_continuation(
                self.llm, prompt, agent_name="UserStory_FixSingleIssue",
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
                files[planning_asset_path("user_stories.md")] = updated_content
                logger.info("UserStory: fix applied (single-issue) — %s", fix_desc[:120])
            elif file_updates:
                for path, content in file_updates.items():
                    if content and isinstance(content, str) and content.strip():
                        files[path] = content
                        logger.info("UserStory: fix applied (single-issue) — %s", fix_desc[:120])
                        break

            return ToolAgentPhaseOutput(
                summary=fix_desc or f"User story issue addressed: {issue[:50]}",
                files=files,
                resolved=resolved or bool(files),
                metadata={"root_cause": raw.get("root_cause", "")},
            )

        except Exception as e:
            logger.warning("UserStory fix_single_issue failed: %s", e)
            return ToolAgentPhaseOutput(
                summary=f"Fix failed: {str(e)[:50]}",
                resolved=False,
            )

    def deliver(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Deliver phase: finalize user story documentation."""
        return ToolAgentPhaseOutput(
            summary="User story documentation finalized.",
            recommendations=["Ensure user stories are committed to repo"],
            hierarchy=inp.hierarchy,
        )
