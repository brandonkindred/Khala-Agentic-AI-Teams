"""
Models for the codegen team (config-driven backend + frontend code generation).

Workflow models are shared with both stacks in
``software_engineering_team.shared.v2_models`` and re-exported here. Only
``ToolAgentKind`` (the merged backend+frontend tool-agent routing registry),
the workflow result envelope, and ``MicrotaskReviewConfig`` are
defined locally.

``ToolAgentKind`` used to be a distinct ``(str, Enum)`` per team solely to
avoid an import cycle between team-local models and
``shared/v2_team_config.py`` (``V2TeamConfig.tool_agent_kinds`` stores plain
strings, never the enum type itself). Now that both stacks live under one
``codegen_team`` package, that constraint no longer applies, so this module
defines a single superset enum; each stack's ``V2TeamConfig.tool_agent_kinds``
still selects only the subset of member *values* it actually wires (see
``stacks/backend/profile.py`` / ``stacks/frontend/profile.py``), and
``ConfigDrivenV2DevelopmentAgent._validate_tool_agents`` continues to enforce
that the built roster matches the declared subset exactly.
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
    "CodegenWorkflowResult",
    "ToolAgentInput",
    "ToolAgentPhaseInput",
    "ToolAgentPhaseOutput",
    "ToolAgentOutput",
    "MicrotaskReviewConfig",
    "MicrotaskReviewFailedError",
]

# ---------------------------------------------------------------------------
# Enum (merged backend + frontend tool-agent routing registry)
# ---------------------------------------------------------------------------


class ToolAgentKind(str, Enum):
    """Identifies which tool agent a microtask should be routed to.

    Superset of the former backend-only and frontend-only ``ToolAgentKind``
    enums. A concrete stack's ``V2TeamConfig.tool_agent_kinds`` (see
    ``stacks/backend/profile.py`` / ``stacks/frontend/profile.py``) declares
    which subset of these member values that stack actually builds and
    validates against.
    """

    # Shared by both stacks
    AUTH = "auth"
    API_OPENAPI = "api_openapi"
    DOCUMENTATION = "documentation"
    TESTING_QA = "testing_qa"
    SECURITY = "security"
    GIT_BRANCH_MANAGEMENT = "git_branch_management"
    BUILD_SPECIALIST = "build_specialist"
    GENERAL = "general"

    # Backend-only
    DATA_ENGINEERING = "data_engineering"

    # Frontend-only
    STATE_MANAGEMENT = "state_management"
    UI_DESIGN = "ui_design"
    BRANDING_THEME = "branding_theme"
    UX_USABILITY = "ux_usability"
    ACCESSIBILITY = "accessibility"
    PERFORMANCE = "performance"
    ARCHITECTURE = "architecture"
    LINTER = "linter"


# ---------------------------------------------------------------------------
# Workflow result
# ---------------------------------------------------------------------------


class CodegenWorkflowResult(BaseModel):
    """
    Full result of the codegen team's autonomous workflow (either stack).

    Captures outcome of the 7-phase lifecycle:
    Setup → Planning → Execution → Review → Problem-solving → Documentation → Deliver.
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
# Per-microtask review configuration (adopts backend's superset of per-phase
# retry limits for both stacks; frontend's execution bindings compute their
# retry cap from ``max_retries`` and do not read the per-phase fields, so
# carrying them unused is harmless).
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
