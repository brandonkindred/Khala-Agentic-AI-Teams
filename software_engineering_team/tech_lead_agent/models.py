"""Models for the Tech Lead agent."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from shared.models import ProductRequirements, SystemArchitecture, Task, TaskAssignment


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


class TechLeadOutput(BaseModel):
    """Output from the Tech Lead agent."""

    assignment: Optional[TaskAssignment] = Field(
        default=None,
        description="Task assignment; None when spec_clarification_needed is True",
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
