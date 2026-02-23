"""
Models for the backend-code-v2 team.

All types are defined from scratch — no reuse of ``backend_agent`` models.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


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
    DELIVER = "deliver"


class MicrotaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
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
    depends_on: List[str] = Field(default_factory=list, description="IDs of prerequisite microtasks")
    output_files: Dict[str, str] = Field(
        default_factory=dict,
        description="Files produced by this microtask (path → content)",
    )
    notes: str = Field(default="", description="Free-form notes or recommendations from the tool agent")


# ---------------------------------------------------------------------------
# Phase results
# ---------------------------------------------------------------------------

class SetupResult(BaseModel):
    """Output of the Setup phase (Backend Tech Lead)."""

    repo_initialized: bool = Field(default=False)
    readme_created: bool = Field(default=False)
    branch_created: bool = Field(default=False)
    master_renamed_to_main: bool = Field(default=False)
    summary: str = Field(default="")


class PlanningResult(BaseModel):
    """Output of the Planning phase."""

    microtasks: List[Microtask] = Field(default_factory=list)
    language: str = Field(default="python", description="Detected language: python or java")
    summary: str = Field(default="")


class ExecutionResult(BaseModel):
    """Aggregated output of the Execution phase."""

    files: Dict[str, str] = Field(default_factory=dict, description="All files produced")
    microtasks: List[Microtask] = Field(default_factory=list, description="Microtasks with updated status")
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


class ProblemSolvingResult(BaseModel):
    """Output of the Problem-solving phase."""

    fixes_applied: List[Dict[str, Any]] = Field(default_factory=list)
    files: Dict[str, str] = Field(default_factory=dict, description="Updated files after fixes")
    summary: str = Field(default="")
    resolved: bool = Field(default=False)


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
    spec_context: str = Field(default="")
    language: str = Field(default="python")


class ToolAgentPhaseInput(BaseModel):
    """Input for tool agent phase methods (plan, review, problem_solve, deliver)."""

    phase: Phase = Field(default=Phase.PLANNING)
    microtask: Optional[Microtask] = None
    repo_path: str = Field(default="")
    existing_code: str = Field(default="")
    spec_context: str = Field(default="")
    language: str = Field(default="python")
    current_files: Dict[str, str] = Field(default_factory=dict)
    review_issues: List[ReviewIssue] = Field(default_factory=list)
    task_title: str = Field(default="")
    task_description: str = Field(default="")
    task_id: str = Field(default="")
    feature_branch_name: Optional[str] = Field(default=None)


class ToolAgentPhaseOutput(BaseModel):
    """Output from tool agent phase methods (plan, review, problem_solve, deliver)."""

    recommendations: List[str] = Field(default_factory=list)
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
