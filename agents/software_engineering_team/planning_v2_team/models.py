"""
Models for the planning-v2 team.

All types are defined from scratch — no reuse of planning_team or project_planning_agent.
"""

from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class Phase(str, Enum):
    """Lifecycle phases of the planning-v2 workflow (Product Planning Tool Agents)."""

    SPEC_REVIEW_GAP = "spec_review_gap"
    PLANNING = "planning"
    IMPLEMENTATION = "implementation"
    REVIEW = "review"
    PROBLEM_SOLVING = "problem_solving"
    DELIVER = "deliver"


class PlanningRole(str, Enum):
    """Agent roles that participate in planning phases (from Product Planning Tool Agents matrix)."""

    SYSTEM_DESIGN = "system_design"
    ARCHITECTURE_HIGH_LEVEL = "architecture_high_level"
    USER_STORY_CREATION = "user_story_creation"
    DEVOPS = "devops"
    UI_DESIGN = "ui_design"
    UX_DESIGN = "ux_design"
    TASK_CLASSIFICATION = "task_classification"
    TASK_DEPENDENCY_ANALYZER = "task_dependency_analyzer"


# ---------------------------------------------------------------------------
# Phase result types (optional, for passing data between phases)
# ---------------------------------------------------------------------------


class SpecReviewResult(BaseModel):
    """Output of Spec Review and Gap analysis phase."""

    gaps: List[str] = Field(default_factory=list)
    open_questions: List[str] = Field(default_factory=list)
    system_design_notes: str = Field(default="")
    architecture_notes: str = Field(default="")
    summary: str = Field(default="")


class PlanningPhaseResult(BaseModel):
    """Output of Planning phase (high-level plan, milestones, user stories)."""

    milestones: List[str] = Field(default_factory=list)
    user_stories: List[str] = Field(default_factory=list)
    high_level_plan: str = Field(default="")
    summary: str = Field(default="")


class ImplementationPhaseResult(BaseModel):
    """Output of Implementation phase (planning assets created/updated)."""

    assets_created: List[str] = Field(default_factory=list)
    assets_updated: List[str] = Field(default_factory=list)
    summary: str = Field(default="")


class ReviewPhaseResult(BaseModel):
    """Output of Review phase (cohesion, spec alignment)."""

    passed: bool = Field(default=False)
    issues: List[str] = Field(default_factory=list)
    summary: str = Field(default="")


class ProblemSolvingPhaseResult(BaseModel):
    """Output of Problem-solving phase (fixes applied)."""

    fixes_applied: List[str] = Field(default_factory=list)
    resolved: bool = Field(default=False)
    summary: str = Field(default="")


class DeliverPhaseResult(BaseModel):
    """Output of Deliver phase."""

    committed: bool = Field(default=False)
    summary: str = Field(default="")


# ---------------------------------------------------------------------------
# Workflow result
# ---------------------------------------------------------------------------


class PlanningV2WorkflowResult(BaseModel):
    """
    Full result of the planning-v2 team's workflow.

    Captures outcome of the 6-phase lifecycle.
    """

    success: bool = Field(default=False)
    current_phase: Optional[Phase] = None
    summary: str = Field(default="")
    failure_reason: str = Field(default="")
    spec_review_result: Optional[SpecReviewResult] = None
    planning_result: Optional[PlanningPhaseResult] = None
    implementation_result: Optional[ImplementationPhaseResult] = None
    review_result: Optional[ReviewPhaseResult] = None
    problem_solving_result: Optional[ProblemSolvingPhaseResult] = None
    deliver_result: Optional[DeliverPhaseResult] = None
