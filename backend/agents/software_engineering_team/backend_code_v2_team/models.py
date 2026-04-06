"""
Models for the backend-code-v2 team.

All types are defined from scratch — no reuse of ``backend_agent`` models.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from software_engineering_team.shared.models import ToolRecommendation

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Phase(str, Enum):
    """Lifecycle phases of the backend-code-v2 workflow."""

    SETUP = "setup"
    PLANNING = "planning"
    EXECUTION = "execution"
    REVIEW = "review"
    PROBLEM_SOLVING = "problem_solving"
    DOCUMENTATION = "documentation"
    DELIVER = "deliver"


class MicrotaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    IN_CODE_REVIEW = "in_code_review"
    IN_QA_TESTING = "in_qa_testing"
    IN_SECURITY_TESTING = "in_security_testing"
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
    CICD = "cicd"
    CONTAINERIZATION = "containerization"
    DOCUMENTATION = "documentation"
    TESTING_QA = "testing_qa"
    SECURITY = "security"
    GIT_BRANCH_MANAGEMENT = "git_branch_management"
    BUILD_SPECIALIST = "build_specialist"
    GENERAL = "general"


# ---------------------------------------------------------------------------
# Microtask
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
# Phase results
# ---------------------------------------------------------------------------


class SetupResult(BaseModel):
    """Output of the Setup phase (Backend Tech Lead)."""

    repo_initialized: bool = Field(default=False)
    readme_created: bool = Field(default=False)
    branch_created: bool = Field(default=False)
    master_renamed_to_main: bool = Field(default=False)
    linting_configured: bool = Field(
        default=False, description="Whether linting tools are configured in the project"
    )
    testing_configured: bool = Field(
        default=False, description="Whether testing tools are configured in the project"
    )
    summary: str = Field(default="")


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


class ReviewIssue(BaseModel):
    """A single issue surfaced during Review."""

    source: str = Field(default="", description="e.g. code_review, qa, security, build, lint")
    severity: str = Field(default="medium", description="critical, high, medium, low, info")
    description: str = Field(default="")
    file_path: str = Field(default="")
    recommendation: str = Field(default="")


class ReviewResult(BaseModel):
    """Output of the Review phase."""

    passed: bool = Field(default=False)
    issues: List[ReviewIssue] = Field(default_factory=list)
    build_ok: bool = Field(default=False)
    lint_ok: bool = Field(default=False)
    summary: str = Field(default="")


class PhaseReviewResult(BaseModel):
    """Output of a single phase-specific review (code review, QA, security, or documentation)."""

    passed: bool = Field(default=False)
    issues: List[ReviewIssue] = Field(default_factory=list)
    summary: str = Field(default="")
    phase_name: str = Field(
        default="", description="Name of the phase: code_review, qa, security, documentation"
    )


class ProblemSolvingResult(BaseModel):
    """Output of the Problem-solving phase."""

    fixes_applied: List[Dict[str, Any]] = Field(default_factory=list)
    files: Dict[str, str] = Field(default_factory=dict, description="Updated files after fixes")
    summary: str = Field(default="")
    resolved: bool = Field(default=False)
    unresolved_issues: List[ReviewIssue] = Field(
        default_factory=list,
        description="Issues still unresolved after per-issue fix attempts (escalate to planning for fix microtasks)",
    )


class DocumentationPhaseResult(BaseModel):
    """Output of the Documentation phase."""

    files: Dict[str, str] = Field(
        default_factory=dict, description="All files with documentation updates"
    )
    iterations: int = Field(default=0, description="Number of review/fix iterations")
    issues_fixed: int = Field(default=0, description="Total documentation issues fixed")
    summary: str = Field(default="")


class BatchFixResult(BaseModel):
    """Result from batch fixing all issues from a review phase."""

    files: Dict[str, str] = Field(default_factory=dict, description="Updated files after batch fix")
    issues_addressed: List[str] = Field(
        default_factory=list,
        description="List of issue descriptions that were addressed",
    )
    issues_count: int = Field(default=0, description="Total number of issues sent for fixing")
    addressed_count: int = Field(default=0, description="Number of issues successfully addressed")
    summary: str = Field(default="")
    success: bool = Field(default=False, description="True if all issues were addressed")


class DocumentationSelfReviewResult(BaseModel):
    """Result from documentation self-review loop."""

    documentation: Dict[str, str] = Field(
        default_factory=dict,
        description="Final documentation files after self-review iterations",
    )
    iterations: int = Field(default=0, description="Number of self-review iterations performed")
    final_quality_score: float = Field(
        default=0.0,
        description="Quality score from final iteration (0.0-1.0)",
    )
    improvements_made: List[str] = Field(
        default_factory=list,
        description="List of improvements made across all iterations",
    )
    summary: str = Field(default="")


class DeliverResult(BaseModel):
    """Output of the Deliver phase."""

    branch_name: str = Field(default="")
    merged: bool = Field(default=False)
    commit_messages: List[str] = Field(default_factory=list)
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
# Tool-agent I/O base types
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


class ToolAgentPhaseOutput(BaseModel):
    """Output from tool agent phase methods (plan, review, problem_solve, deliver)."""

    recommendations: List[str] = Field(default_factory=list)
    tool_recommendations: List[ToolRecommendation] = Field(
        default_factory=list,
        description="Structured tool/service recommendations with pricing, licensing, and adoption details.",
    )
    issues: List[ReviewIssue] = Field(default_factory=list)
    files: Dict[str, str] = Field(default_factory=dict)
    summary: str = Field(default="")
    success: bool = Field(default=True)


class ToolAgentOutput(BaseModel):
    """Base output for all team-owned tool agents (Execution phase)."""

    files: Dict[str, str] = Field(default_factory=dict)
    recommendations: List[str] = Field(default_factory=list)
    summary: str = Field(default="")
    success: bool = Field(default=True)


# ---------------------------------------------------------------------------
# Per-microtask review configuration
# ---------------------------------------------------------------------------


class MicrotaskReviewConfig(BaseModel):
    """Configuration for per-microtask review gates with per-phase retry limits."""

    max_retries: int = Field(
        default=3,
        description="Max problem-solving attempts per microtask before marking as failed (legacy, used if per-phase not set)",
    )
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
    on_failure: Literal["stop", "skip_continue"] = Field(
        default="stop",
        description="Behavior when max retries exceeded: 'stop' aborts workflow, 'skip_continue' proceeds to next microtask",
    )
    security_failure_always_stops: bool = Field(
        default=True,
        description="When True, security review failures always stop the workflow regardless of on_failure setting",
    )


class MicrotaskReviewFailedError(Exception):
    """Raised when a microtask fails review and on_failure='stop'."""

    def __init__(self, microtask: "Microtask", review_result: "ReviewResult") -> None:
        self.microtask = microtask
        self.review_result = review_result
        super().__init__(f"Microtask {microtask.id} failed review after max retries")
