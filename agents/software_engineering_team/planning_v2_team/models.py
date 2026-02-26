"""
Models for the planning-v2 team.

All types are defined from scratch — no reuse of planning_team or project_planning_agent.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from shared.models import Initiative, Epic, StoryPlan, TaskPlan, PlanningHierarchy


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


class ToolAgentKind(str, Enum):
    """Tool agent types for the planning-v2 team (8 tool agents)."""

    SYSTEM_DESIGN = "system_design"
    ARCHITECTURE = "architecture"
    USER_STORY = "user_story"
    DEVOPS = "devops"
    UI_DESIGN = "ui_design"
    UX_DESIGN = "ux_design"
    TASK_CLASSIFICATION = "task_classification"
    TASK_DEPENDENCY = "task_dependency"


# ---------------------------------------------------------------------------
# Tool Agent Phase Input/Output
# ---------------------------------------------------------------------------


class ToolAgentPhaseInput(BaseModel):
    """Input to a tool agent's phase method (plan/execute/review/problem_solve/deliver)."""

    spec_content: str = Field(default="")
    inspiration_content: str = Field(default="")
    repo_path: str = Field(default="")
    spec_review_result: Optional[Any] = None
    planning_result: Optional[Any] = None
    implementation_result: Optional[Any] = None
    review_result: Optional[Any] = None
    current_files: Dict[str, str] = Field(default_factory=dict)
    review_issues: List[str] = Field(default_factory=list)
    hierarchy: Optional[PlanningHierarchy] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ToolAgentPhaseOutput(BaseModel):
    """Output from a tool agent's phase method."""

    summary: str = Field(default="")
    recommendations: List[str] = Field(default_factory=list)
    issues: List[str] = Field(default_factory=list)
    files: Dict[str, str] = Field(default_factory=dict)
    hierarchy: Optional[PlanningHierarchy] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Open Question Models (for iterative spec review)
# ---------------------------------------------------------------------------


class QuestionOption(BaseModel):
    """A selectable option for an open question."""

    id: str = Field(description="Unique option identifier")
    label: str = Field(description="Display text for this option")
    is_default: bool = Field(
        default=False, description="Whether this is the recommended default"
    )


class OpenQuestion(BaseModel):
    """An open question with selectable options."""

    id: str = Field(description="Unique question identifier")
    question_text: str = Field(description="The question text")
    context: str = Field(
        default="", description="Additional context explaining why this matters"
    )
    options: List[QuestionOption] = Field(
        default_factory=list, description="2-3 selectable options"
    )
    source: str = Field(default="spec_review", description="Origin of this question")


class AnsweredQuestion(BaseModel):
    """A question that has been answered (by user or default)."""

    question_id: str = Field(description="ID of the original question")
    question_text: str = Field(description="The question text")
    selected_option_id: str = Field(description="ID of the selected option")
    selected_answer: str = Field(description="Text of the selected answer")
    was_default: bool = Field(
        default=False, description="Whether the default was applied"
    )
    other_text: str = Field(
        default="", description="Custom text if 'other' was selected"
    )


# ---------------------------------------------------------------------------
# Phase result types (optional, for passing data between phases)
# ---------------------------------------------------------------------------


class SpecReviewResult(BaseModel):
    """Output of Product Requirement Analysis phase."""

    issues: List[str] = Field(
        default_factory=list, description="Issues identified in the spec"
    )
    product_gaps: List[str] = Field(
        default_factory=list, description="Product gaps highlighted"
    )
    open_questions: List[OpenQuestion] = Field(
        default_factory=list, description="Structured questions requiring clarification"
    )
    plan_summary: str = Field(default="", description="Brief plan output summary")
    summary: str = Field(default="", description="Overall summary")


class PlanningPhaseResult(BaseModel):
    """Output of Product Planning phase with structured sections."""

    goals_vision: str = Field(default="", description="Goals and vision statement")
    constraints_limitations: str = Field(
        default="", description="Constraints and limitations"
    )
    key_features: List[str] = Field(
        default_factory=list, description="Key features list"
    )
    milestones: List[str] = Field(default_factory=list, description="Project milestones")
    architecture: str = Field(default="", description="Architecture overview")
    maintainability: str = Field(
        default="", description="Maintainability considerations"
    )
    security: str = Field(default="", description="Security requirements")
    file_system: str = Field(default="", description="File/folder structure plan")
    styling: str = Field(default="", description="UI/UX styling guidelines")
    dependencies: List[str] = Field(
        default_factory=list, description="External dependencies"
    )
    microservices: str = Field(default="", description="Microservices breakdown")
    others: str = Field(default="", description="Additional notes")
    summary: str = Field(default="", description="Overall planning summary")
    hierarchy: Optional[PlanningHierarchy] = None


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
    user_answers: Dict[str, Any] = Field(
        default_factory=dict,
        description="User answers to open questions submitted during the workflow.",
    )
