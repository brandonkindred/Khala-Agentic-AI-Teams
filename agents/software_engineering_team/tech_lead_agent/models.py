"""Models for the Tech Lead agent."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from software_engineering_team.shared.models import PlanningHierarchy, ProductRequirements, SystemArchitecture, Task, TaskAssignment


class SpecRequirementMapping(BaseModel):
    """Maps a spec requirement to the tasks that implement it."""

    spec_item: str
    task_ids: List[str] = Field(default_factory=list)


class TechLeadInput(BaseModel):
    """Input for the Tech Lead agent."""

    requirements: ProductRequirements
    architecture: Optional[SystemArchitecture] = Field(
        None,
        description="System architecture from Architecture Expert (required for task breakdown)",
    )
    repo_path: Optional[str] = Field(
        None,
        description="Path to the git repo; Tech Lead uses this for context and orchestration",
    )
    spec_content: Optional[str] = Field(
        None,
        description="Full content of initial_spec.md; used to generate the build plan",
    )
    existing_tasks: Optional[List[Task]] = Field(
        None,
        description="Existing tasks to extend or reprioritize",
    )
    existing_codebase: Optional[str] = Field(
        None,
        description="Existing code in the repository; Tech Lead uses this to understand current state before planning",
    )
    project_overview: Optional[Dict[str, Any]] = Field(
        None,
        description="Project overview from ProjectPlanningAgent (goals, milestones, delivery strategy)",
    )
    open_questions: Optional[List[str]] = Field(
        None,
        description="Open questions from Spec Intake needing resolution; Tech Lead resolves with best-practice defaults",
    )
    assumptions: Optional[List[str]] = Field(
        None,
        description="Assumptions from Spec Intake; Tech Lead may extend when resolving open questions",
    )
    resolved_questions: Optional[List[Dict[str, str]]] = Field(
        None,
        description="User-provided answers from clarification chat; when set, use these instead of resolving with defaults",
    )
    planning_hierarchy: Optional[PlanningHierarchy] = Field(
        None,
        description="Pre-existing planning hierarchy from Planning V2; Tech Lead uses this for creating the development plan instead of generating new tasks",
    )
    plan_artifacts_content: Optional[str] = Field(
        None,
        description="Concatenated content of planning artifacts from /plan folder; Tech Lead uses this for context when generating detailed plans",
    )


class TechLeadOutput(BaseModel):
    """Output from the Tech Lead agent."""

    assignment: Optional[TaskAssignment] = Field(
        default=None,
        description="Task assignment (flattened from hierarchy); None when spec_clarification_needed is True",
    )
    planning_hierarchy: Optional[PlanningHierarchy] = Field(
        default=None,
        description="Full Initiative -> Epic -> Story hierarchy from planning",
    )
    summary: str = ""
    requirement_task_mapping: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Maps each spec requirement/acceptance criterion to task IDs that implement it",
    )
    spec_clarification_needed: bool = Field(
        default=False,
        description="When True, the spec is incomplete or ambiguous; do not proceed with tasks",
    )
    clarification_questions: List[str] = Field(
        default_factory=list,
        description="Specific questions for the product owner when spec_clarification_needed is True",
    )
