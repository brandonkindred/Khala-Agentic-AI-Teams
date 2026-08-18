"""
Models for the backend-code-v2 team.

Workflow models are shared with frontend v2 in
``software_engineering_team.shared.v2_models`` and re-exported here. Only
``ToolAgentKind`` (backend-specific tool-agent routing), the workflow result
envelope, and backend-specific ``MicrotaskReviewConfig`` knobs are defined
locally; this team's language default (``"python"``) is enforced via
``PROFILE.default_language`` in ``phases/_profile.py``, not a model field
default.
"""

from __future__ import annotations

from enum import Enum
from typing import Dict, Optional

from pydantic import BaseModel, Field

from software_engineering_team.shared.v2_models import (
    BaseMicrotaskReviewConfig,
    BatchFixResult,
    DeliverResult,
    DocumentationPhaseResult,
    DocumentationSelfReviewResult,
    ExecutionResult,
    Microtask,
    MicrotaskStatus,
    Phase,
    PhaseReviewResult,
    PlanningResult,
    ProblemSolvingResult,
    ReviewIssue,
    ReviewResult,
    SetupResult,
    ToolAgentInput,
    ToolAgentOutput,
    ToolAgentPhaseInput,
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
# Enum (backend-specific tool-agent routing)
# ---------------------------------------------------------------------------


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
