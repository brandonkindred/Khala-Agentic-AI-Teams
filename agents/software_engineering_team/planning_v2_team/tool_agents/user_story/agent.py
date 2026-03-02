"""
User Story tool agent for planning-v2.

Participates in phases: Planning, Implementation, Review, Problem Solving, Deliver.
Produces the hierarchical output: Initiative -> Epic -> Story -> Task.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from shared.models import Initiative, Epic, StoryPlan, TaskPlan, PlanningHierarchy

from ...models import ToolAgentPhaseInput, ToolAgentPhaseOutput
from ..json_utils import parse_json_with_recovery, default_decompose_by_sections, complete_with_continuation

if TYPE_CHECKING:
    from shared.llm import LLMClient

logger = logging.getLogger(__name__)


def _merge_user_story_results(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge user story results from multiple chunks."""
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
    return merged

USER_STORY_PLANNING_PROMPT = """You are a Product Planning expert specializing in user story creation and prioritization.

Given the specification below, create a hierarchical plan with:
- Initiatives (high-level goals)
- Epics (features within each initiative)
- Stories (detailed user stories with acceptance criteria)
- Tasks (actionable work items assigned to teams)

IMPORTANT: Stories and Tasks must be highly detailed with:
- Clear descriptions
- Acceptance criteria
- Examples where appropriate
- Team assignment (frontend, backend, devops, qa)

TASK SIZING - CRITICAL:
Each task should be estimated as a 2 or 3 on the Fibonacci scale (1, 2, 3, 5, 8, 13...) during sprint planning.

A 2-3 point task typically:
- Can be completed in 1-2 days
- Has a focused objective with clear boundaries
- Touches 2-5 files
- May involve related concerns that logically belong together (e.g., "Create login form with validation" is acceptable)
- Is independently testable

If a task feels like a 5 or larger, BREAK IT DOWN into smaller tasks. For example:
- BAD: "Implement user authentication" (too broad, likely 8+ points)
- GOOD: Split into: "Create login form with validation", "Create /login API endpoint with JWT", "Add auth middleware and session storage"

If a task feels like a 1, consider combining it with a closely related task.

Specification:
---
{spec_content}
---

Plan Summary: {plan_summary}

Respond with JSON:
{{
  "initiatives": [
    {{
      "id": "INIT-1",
      "title": "Initiative title",
      "description": "What this initiative achieves",
      "epics": [
        {{
          "id": "EPIC-1",
          "title": "Epic title",
          "description": "Feature description",
          "acceptance_criteria": ["criterion 1", "criterion 2"],
          "stories": [
            {{
              "id": "STORY-1",
              "title": "Story title",
              "description": "As a [user], I want [feature] so that [benefit]",
              "acceptance_criteria": ["Given/When/Then criteria"],
              "example": "Example usage scenario",
              "tasks": [
                {{
                  "id": "TASK-1",
                  "title": "Task title",
                  "description": "Detailed task description",
                  "acceptance_criteria": ["task completion criteria"],
                  "assigned_team": "frontend|backend|devops|qa",
                  "example": "Implementation example if applicable",
                  "complexity_points": 2
                }}
              ]
            }}
          ]
        }}
      ]
    }}
  ],
  "summary": "Brief planning summary"
}}
"""

USER_STORY_REVIEW_PROMPT = """You are a Product Planning expert. Review these user stories and tasks for:
1. Completeness of acceptance criteria
2. Clear team assignments
3. Task granularity - each task should be Fibonacci 2-3 points (focused objective, completable in 1-2 days)
4. Dependency clarity
5. Tasks that are too large (5+ points) should be flagged for splitting
6. Tasks that are too small (1 point) should be flagged for combining with related tasks

Artifacts:
---
{artifacts}
---

Respond with JSON:
{{
  "passed": true or false,
  "issues": ["list of issues found"],
  "recommendations": ["improvements"],
  "summary": "brief summary"
}}
"""

USER_STORY_PLANNING_CHUNK_PROMPT = """You are a Product Planning expert. Analyze this SECTION of a specification for user stories:

SECTION:
---
{chunk_content}
---

Create user stories and tasks for THIS section only.

TASK SIZING: Each task should be Fibonacci 2-3 points (1-2 day effort, focused objective, 2-5 files). Break 5+ point tasks into smaller tasks; combine 1-point tasks with related work.

Respond with concise JSON:
{{
  "initiatives": [
    {{
      "id": "INIT-1",
      "title": "Initiative for this section",
      "description": "What this achieves",
      "epics": [
        {{
          "id": "EPIC-1",
          "title": "Epic title",
          "description": "Feature description",
          "acceptance_criteria": ["criterion"],
          "stories": [
            {{
              "id": "STORY-1",
              "title": "Story title",
              "description": "As a user, I want...",
              "acceptance_criteria": ["Given/When/Then"],
              "tasks": [
                {{
                  "id": "TASK-1",
                  "title": "Task title",
                  "description": "Task details",
                  "assigned_team": "frontend|backend|devops|qa",
                  "complexity_points": 2
                }}
              ]
            }}
          ]
        }}
      ]
    }}
  ],
  "summary": "Brief summary"
}}
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

Analyze and fix this issue. If the issue relates to task sizing, acceptance criteria, or missing stories/tasks, provide the complete updated file content.

Respond with JSON:
{{
  "root_cause": "why this issue exists",
  "fix_description": "what you are changing to fix it",
  "resolved": true or false,
  "updated_content": "the complete updated file content (or empty string if no change needed)"
}}
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
        data = parse_json_with_recovery(
            self.llm,
            prompt,
            agent_name="UserStory",
            decompose_fn=default_decompose_by_sections,
            merge_fn=_merge_user_story_results,
            original_content=spec_content,
            chunk_prompt_template=USER_STORY_PLANNING_CHUNK_PROMPT,
        )
        
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
        
        If review_issues are provided, this agent handles fixes however it sees fit:
        - May process all issues in one prompt
        - May batch similar issues together
        - May handle issues one-by-one
        """
        hierarchy = inp.hierarchy
        all_files: Dict[str, str] = {}
        fixes_applied: List[str] = []
        
        story_issues = [
            i for i in inp.review_issues
            if any(kw in i.lower() for kw in ["story", "task", "epic", "user", "acceptance", "criteria"])
        ]
        
        if story_issues and self.llm:
            logger.info("UserStory: handling %d review issues", len(story_issues))
            for issue in story_issues:
                result = self.fix_single_issue(issue, inp)
                if result.files:
                    all_files.update(result.files)
                    fixes_applied.append(result.summary)
                if result.hierarchy:
                    hierarchy = result.hierarchy
            logger.info("UserStory: fixed %d/%d issues", len(fixes_applied), len(story_issues))
        
        if not hierarchy:
            return ToolAgentPhaseOutput(
                summary="User Story execute skipped (no hierarchy).",
                recommendations=fixes_applied if fixes_applied else [],
            )
        
        content = _hierarchy_to_markdown(hierarchy)
        all_files["plan/user_stories.md"] = content
        
        summary = "User story artifacts generated."
        if fixes_applied:
            summary = f"User story artifacts generated. Fixed {len(fixes_applied)} review issues."
        
        return ToolAgentPhaseOutput(
            summary=summary,
            files=all_files,
            hierarchy=hierarchy,
            recommendations=fixes_applied if fixes_applied else [],
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
        data = parse_json_with_recovery(self.llm, prompt, agent_name="UserStory")
        
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

        current_artifact = inp.current_files.get("plan/user_stories.md", "")
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
            raw = complete_with_continuation(
                llm=self.llm,
                prompt=prompt,
                mode="json",
                agent_name="UserStory_FixSingleIssue",
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
                files["plan/user_stories.md"] = updated_content
                logger.info("UserStory: fix applied — %s", fix_desc[:60])

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
