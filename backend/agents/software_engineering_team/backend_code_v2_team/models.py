"""
Models for the backend-code-v2 team.

Structurally identical workflow models are shared with frontend v2 in
``software_engineering_team.shared.v2_models`` and re-exported here. Only the
types that bind a backend-specific ``ToolAgentKind``/``MicrotaskStatus`` enum,
the backend ``Microtask``, a backend language default, or backend-specific
``MicrotaskReviewConfig`` knobs are defined locally.
"""

from __future__ import annotations

from enum import Enum
from typing import Dict, List, Optional

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
    "BackendCodeV2WorkflowResult",
    "ToolAgentInput",
    "ToolAgentPhaseInput",
    "ToolAgentPhaseOutput",
    "ToolAgentOutput",
    "MicrotaskReviewConfig",
    "MicrotaskReviewFailedError",
]

# ---------------------------------------------------------------------------
# Enums (backend-specific members)
# ---------------------------------------------------------------------------


class MicrotaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    IN_CODE_REVIEW = "in_code_review"
    IN_QA_TESTING = "in_qa_testing"
    IN_SECURITY_TESTING = "in_security_testing"
    IN_QA_SECURITY_TESTING = "in_qa_security_testing"
    IN_REVIEW = "in_review"
    IN_DOCUMENTATION = "in_documentation"
    COMPLETED = "completed"
    FAILED = "failed"
    REVIEW_FAILED = "review_failed"
    SKIPPED = "skipped"


class ToolAgentKind(str, Enum):
    """Identifies which tool agent a microtask should be routed to."""

    DATA_ENGINEERING = "data_engineering"
    API_OPENAPI = "api_openapi"
    AUTH = "auth"
    DOCUMENTATION = "documentation"
    TESTING_QA = "testing_qa"
    SECURITY = "security"
    GIT_BRANCH_MANAGEMENT = "git_branch_management"
    BUILD_SPECIALIST = "build_specialist"
    GENERAL = "general"


# ---------------------------------------------------------------------------
# Microtask (binds backend enums)
# ---------------------------------------------------------------------------


class Microtask(BaseModel):
    """A single unit of work inside the Planning phase output."""

    id: str = Field(..., description="Unique kebab-case ID, e.g. mt-create-user-model")
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
# Phase results that reference the backend Microtask / language default
# ---------------------------------------------------------------------------


class PlanningResult(BaseModel):
    """Output of the Planning phase."""

    microtasks: List[Microtask] = Field(default_factory=list)
    language: str = Field(default="python", description="Detected language: python or java")
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


class BackendCodeV2WorkflowResult(BaseModel):
    """
    Full result of the backend-code-v2 team's autonomous workflow.

    Captures outcome of the 5-phase lifecycle:
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
# Tool-agent I/O types that reference the backend Microtask / language default
# ---------------------------------------------------------------------------


class ToolAgentInput(BaseModel):
    """Base input for all team-owned tool agents (Execution phase)."""

    microtask: Microtask
    repo_path: str = Field(default="")
    existing_code: str = Field(default="")
    language: str = Field(default="python")


class ToolAgentPhaseInput(BaseModel):
    """Input for tool agent phase methods (plan, review, problem_solve, deliver)."""

    phase: Phase = Field(default=Phase.PLANNING)
    microtask: Optional[Microtask] = None
    repo_path: str = Field(default="")
    existing_code: str = Field(default="")
    language: str = Field(default="python")
    current_files: Dict[str, str] = Field(default_factory=dict)
    review_issues: List[ReviewIssue] = Field(default_factory=list)
    task_title: str = Field(default="")
    task_description: str = Field(default="")
    task_id: str = Field(default="")
    feature_branch_name: Optional[str] = Field(default=None)
    spec_context: str = Field(default="", description="Optional spec/context for LLM prompts")


# ---------------------------------------------------------------------------
# Per-microtask review configuration (backend adds per-phase retry limits)
# ---------------------------------------------------------------------------


class MicrotaskReviewConfig(BaseMicrotaskReviewConfig):
    """Configuration for per-microtask review gates with per-phase retry limits."""

    code_review_max_retries: int = Field(
        default=3,
        description="Max fix attempts for code review phase (build + lint + code review)",
    )
    qa_max_retries: int = Field(
        default=3,
        description="Max fix attempts for QA testing phase",
    )
    security_max_retries: int = Field(
        default=3,
        description="Max fix attempts for security testing phase",
    )
    documentation_max_retries: int = Field(
        default=3,
        description="Max fix attempts for documentation phase",
    )
