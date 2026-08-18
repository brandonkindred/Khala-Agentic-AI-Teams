"""
Models for the frontend-code-v2 team.

Workflow models are shared with backend v2 in
``software_engineering_team.shared.v2_models`` and re-exported here. Only
``ToolAgentKind`` (frontend-specific tool-agent routing) and the workflow
result envelope are defined locally; this team's language default
(``"typescript"``) is enforced via ``PROFILE.default_language`` in
``phases/_profile.py``, not a model field default.
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
    "FrontendCodeV2WorkflowResult",
    "ToolAgentInput",
    "ToolAgentPhaseInput",
    "ToolAgentPhaseOutput",
    "ToolAgentOutput",
    "MicrotaskReviewConfig",
    "MicrotaskReviewFailedError",
]

# ---------------------------------------------------------------------------
# Enum (frontend-specific tool-agent routing)
# ---------------------------------------------------------------------------


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
# Per-microtask review configuration
# ---------------------------------------------------------------------------


class MicrotaskReviewConfig(BaseMicrotaskReviewConfig):
    """Configuration for per-microtask review gates (frontend = shared base)."""
