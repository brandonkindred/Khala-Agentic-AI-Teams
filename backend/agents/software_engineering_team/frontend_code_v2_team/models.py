"""
Models for the frontend-code-v2 team.

Structurally identical workflow models are shared with backend v2 in
``software_engineering_team.shared.v2_models`` and re-exported here. Only the
types that bind a frontend-specific ``ToolAgentKind``/``MicrotaskStatus`` enum,
the frontend ``Microtask``, or a frontend language default are defined locally.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from software_engineering_team.shared.v2_models import (
    BaseMicrotaskReviewConfig,
    BatchFixResult,
    DeliverResult,
    DocumentationPhaseResult,
    DocumentationSelfReviewResult,
    Phase,
    PhaseReviewResult,
    ProblemSolvingResult,
    ReviewIssue,
    ReviewResult,
    SetupResult,
    ToolAgentOutput,
    ToolAgentPhaseOutput,
)
from software_engineering_team.shared.v2_models import (
    MicrotaskReviewFailedError as MicrotaskReviewFailedError,
)

__all__ = [
    "Phase",
    "MicrotaskStatus",
    "ToolAgentKind",
    "Microtask",
    "SetupResult",
    "PlanningResult",
    "ExecutionResult",
    "ReviewIssue",
    "ReviewResult",
    "PhaseReviewResult",
    "ProblemSolvingResult",
    "DocumentationPhaseResult",
    "BatchFixResult",
    "DocumentationSelfReviewResult",
    "DeliverResult",
    "FrontendCodeV2WorkflowResult",
    "ToolAgentInput",
    "ToolAgentPhaseInput",
    "ToolAgentPhaseOutput",
    "ToolAgentOutput",
    "MicrotaskReviewConfig",
    "MicrotaskReviewFailedError",
]

# ---------------------------------------------------------------------------
# Enums (frontend-specific members)
# ---------------------------------------------------------------------------


class MicrotaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    IN_QA_SECURITY_TESTING = "in_qa_security_testing"
    IN_DOCUMENTATION = "in_documentation"
    COMPLETED = "completed"
    FAILED = "failed"
    REVIEW_FAILED = "review_failed"
    SKIPPED = "skipped"


class ToolAgentKind(str, Enum):
    """Identifies which tool agent a microtask should be routed to."""

    STATE_MANAGEMENT = "state_management"
    AUTH = "auth"
    API_OPENAPI = "api_openapi"
    DOCUMENTATION = "documentation"
    TESTING_QA = "testing_qa"
    SECURITY = "security"
    GIT_BRANCH_MANAGEMENT = "git_branch_management"
    UI_DESIGN = "ui_design"
    BRANDING_THEME = "branding_theme"
    UX_USABILITY = "ux_usability"
    ACCESSIBILITY = "accessibility"
    PERFORMANCE = "performance"
    ARCHITECTURE = "architecture"
    BUILD_SPECIALIST = "build_specialist"
    LINTER = "linter"
    GENERAL = "general"


# ---------------------------------------------------------------------------
# Microtask (binds frontend enums)
# ---------------------------------------------------------------------------


class Microtask(BaseModel):
    """A single unit of work inside the Planning phase output."""

    id: str = Field(..., description="Unique kebab-case ID, e.g. mt-add-login-component")
    title: str = Field(default="", description="Short human-readable title")
    description: str = Field(default="", description="What needs to be done")
    tool_agent: ToolAgentKind = Field(
        default=ToolAgentKind.GENERAL,
        description="Which tool agent should handle this microtask",
    )
    status: MicrotaskStatus = Field(default=MicrotaskStatus.PENDING)
    depends_on: List[str] = Field(
        default_factory=list, description="IDs of prerequisite microtasks"
    )
    output_files: Dict[str, str] = Field(
        default_factory=dict,
        description="Files produced by this microtask (path → content)",
    )
    notes: str = Field(
        default="", description="Free-form notes or recommendations from the tool agent"
    )


# ---------------------------------------------------------------------------
# Phase results that reference the frontend Microtask / language default
# ---------------------------------------------------------------------------


class PlanningResult(BaseModel):
    """Output of the Planning phase."""

    microtasks: List[Microtask] = Field(default_factory=list)
    language: str = Field(
        default="typescript",
        description="Detected frontend stack: e.g. angular, react, typescript, javascript",
    )
    summary: str = Field(default="")


class ExecutionResult(BaseModel):
    """Aggregated output of the Execution phase."""

    files: Dict[str, str] = Field(default_factory=dict, description="All files produced")
    microtasks: List[Microtask] = Field(
        default_factory=list, description="Microtasks with updated status"
    )
    summary: str = Field(default="")


# ---------------------------------------------------------------------------
# Workflow result
# ---------------------------------------------------------------------------


class FrontendCodeV2WorkflowResult(BaseModel):
    """
    Full result of the frontend-code-v2 team's autonomous workflow.

    Captures outcome of Setup + 5-phase lifecycle:
    Planning → Execution → Review → Problem-solving → Deliver.
    """

    task_id: str = Field(default="", description="ID of the task that was executed")
    success: bool = Field(default=False)
    current_phase: Phase = Field(default=Phase.SETUP)
    iterations_used: int = Field(default=0, description="Number of review/fix iterations")
    setup_result: Optional[SetupResult] = None
    planning_result: Optional[PlanningResult] = None
    execution_result: Optional[ExecutionResult] = None
    review_result: Optional[ReviewResult] = None
    problem_solving_result: Optional[ProblemSolvingResult] = None
    documentation_result: Optional[DocumentationPhaseResult] = None
    deliver_result: Optional[DeliverResult] = None
    final_files: Dict[str, str] = Field(default_factory=dict)
    summary: str = Field(default="")
    failure_reason: str = Field(default="")
    needs_followup: bool = Field(default=False)


# ---------------------------------------------------------------------------
# Tool-agent I/O types that reference the frontend Microtask / language default
# ---------------------------------------------------------------------------


class ToolAgentInput(BaseModel):
    """Base input for all team-owned tool agents (Execution phase)."""

    microtask: Microtask
    repo_path: str = Field(default="")
    existing_code: str = Field(default="")
    language: str = Field(default="typescript")


class ToolAgentPhaseInput(BaseModel):
    """Input for tool agent phase methods (plan, review, problem_solve, deliver)."""

    phase: Phase = Field(default=Phase.PLANNING)
    microtask: Optional[Microtask] = None
    repo_path: str = Field(default="")
    existing_code: str = Field(default="")
    language: str = Field(default="typescript")
    current_files: Dict[str, str] = Field(default_factory=dict)
    review_issues: List[ReviewIssue] = Field(default_factory=list)
    task_title: str = Field(default="")
    task_description: str = Field(default="")
    task_id: str = Field(default="")
    feature_branch_name: Optional[str] = Field(default=None)
    spec_context: str = Field(default="", description="Optional spec/context for LLM prompts")
    build_verifier: Optional[Any] = Field(
        default=None, description="Pre-merge quality gate: build verifier callable"
    )
    build_verify_label: str = Field(default="", description="Pre-merge quality gate: build label")
    linting_tool_agent: Optional[Any] = Field(
        default=None, description="Pre-merge quality gate: linting tool agent"
    )
    lint_agent_type: str = Field(default="", description="Pre-merge quality gate: lint agent_type")


# ---------------------------------------------------------------------------
# Per-microtask review configuration
# ---------------------------------------------------------------------------


class MicrotaskReviewConfig(BaseMicrotaskReviewConfig):
    """Configuration for per-microtask review gates (frontend = shared base)."""
