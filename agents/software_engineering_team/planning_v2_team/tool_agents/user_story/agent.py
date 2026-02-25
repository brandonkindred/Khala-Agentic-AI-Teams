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
from ..json_utils import parse_json_with_recovery

if TYPE_CHECKING:
    from shared.llm import LLMClient

logger = logging.getLogger(__name__)

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

Specification:
---
{spec_content}
---

System Design Notes: {system_design_notes}
Architecture Notes: {architecture_notes}

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
                  "example": "Implementation example if applicable"
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
3. Proper story/task granularity
4. Dependency clarity

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
        
        system_design_notes = ""
        architecture_notes = ""
        if inp.spec_review_result:
            system_design_notes = getattr(inp.spec_review_result, "system_design_notes", "") or ""
            architecture_notes = getattr(inp.spec_review_result, "architecture_notes", "") or ""
        
        prompt = USER_STORY_PLANNING_PROMPT.format(
            spec_content=(inp.spec_content or "")[:10000],
            system_design_notes=system_design_notes[:1500],
            architecture_notes=architecture_notes[:1500],
        )
        data = parse_json_with_recovery(self.llm, prompt, agent_name="UserStory")
        
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
        """Implementation phase: generate user story artifacts."""
        hierarchy = inp.hierarchy
        if not hierarchy:
            return ToolAgentPhaseOutput(summary="User Story execute skipped (no hierarchy).")
        
        content = _hierarchy_to_markdown(hierarchy)
        
        files = {
            "planning_v2/user_stories.md": content,
        }
        
        return ToolAgentPhaseOutput(
            summary="User story artifacts generated.",
            files=files,
            hierarchy=hierarchy,
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
        
        return ToolAgentPhaseOutput(
            summary=f"User Story: {len(story_issues)} issue(s) identified for resolution.",
            recommendations=[f"Address: {issue}" for issue in story_issues[:5]],
        )

    def deliver(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Deliver phase: finalize user story documentation."""
        return ToolAgentPhaseOutput(
            summary="User story documentation finalized.",
            recommendations=["Ensure user stories are committed to repo"],
            hierarchy=inp.hierarchy,
        )
