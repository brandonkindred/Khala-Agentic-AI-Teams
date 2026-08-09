"""
Shared models for the code-v2 teams.

``frontend_code_v2_team`` and ``backend_code_v2_team`` define structurally
identical workflow models. The classes here are the ones that do **not**
reference a team-specific ``Microtask``/``ToolAgentKind``/``MicrotaskStatus``
enum, so they can be defined once and re-exported by each team's ``models.py``.

Team-local (defined per team, not here): ``ToolAgentKind``, ``MicrotaskStatus``,
``Microtask``, ``PlanningResult``, ``ExecutionResult``, ``ToolAgentInput``,
``ToolAgentPhaseInput`` (all bind a team ``Microtask``/enum or a team language
default), and the per-team ``*WorkflowResult``.
``MicrotaskReviewConfig`` subclasses ``BaseMicrotaskReviewConfig`` here.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from shared.dev_models.models import ToolRecommendation

# ---------------------------------------------------------------------------
# Lifecycle phases (identical across teams)
# ---------------------------------------------------------------------------


class Phase(str, Enum):
    """Lifecycle phases of a code-v2 workflow."""

    SETUP = "setup"
    PLANNING = "planning"
    EXECUTION = "execution"
    REVIEW = "review"
    PROBLEM_SOLVING = "problem_solving"
    DOCUMENTATION = "documentation"
    DELIVER = "deliver"


# ---------------------------------------------------------------------------
# Phase results that do not reference a team-local Microtask
# ---------------------------------------------------------------------------


class SetupResult(BaseModel):
    """Output of the Setup phase (Tech Lead)."""

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
    raw_issue_count: Optional[int] = Field(
        default=None,
        description=(
            "Number of code-review issues the LLM fallback found before grounding "
            "filtered any out; None when the LLM fallback never ran (e.g. the external "
            "code_review_agent succeeded) or reported no count."
        ),
    )


class PhaseReviewResult(BaseModel):
    """Output of a single phase-specific review (code review, QA, security, or documentation)."""

    passed: bool = Field(default=False)
    issues: List[ReviewIssue] = Field(default_factory=list)
    summary: str = Field(default="")
    phase_name: str = Field(
        default="", description="Name of the phase: code_review, qa, security, documentation"
    )
    raw_issue_count: Optional[int] = Field(
        default=None,
        description=(
            "Number of issues found by the LLM fallback before grounding filtered "
            "any out; None when the LLM fallback never ran or reported no count."
        ),
    )


class ProblemSolvingResult(BaseModel):
    """Output of the Problem-solving phase."""

    fixes_applied: List[Dict[str, Any]] = Field(default_factory=list)
    files: Dict[str, str] = Field(default_factory=dict, description="Updated files after fixes")
    summary: str = Field(default="")
    resolved: bool = Field(default=False)
    unresolved_issues: List[ReviewIssue] = Field(
        default_factory=list,
        description="Issues still unresolved after fix attempts",
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
    branch_ready: bool = Field(
        default=False,
        description="True when the feature branch has been committed and left ready for external review.",
    )
    merged: bool = Field(default=False)
    commit_messages: List[str] = Field(default_factory=list)
    delivered_files: List[str] = Field(
        default_factory=list, description="Repo-relative file paths written during delivery"
    )
    summary: str = Field(default="")


# ---------------------------------------------------------------------------
# Tool-agent I/O types that do not reference a team-local Microtask
# ---------------------------------------------------------------------------


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
# Per-microtask review configuration (frontend shape; backend extends)
# ---------------------------------------------------------------------------


class BaseMicrotaskReviewConfig(BaseModel):
    """Configuration for per-microtask review gates (shared base)."""

    max_retries: int = Field(
        default=3,
        description="Max problem-solving attempts per microtask before marking as failed",
    )
    on_failure: Literal["stop", "skip_continue"] = Field(
        default="stop",
        description="Behavior when max retries exceeded: 'stop' aborts workflow, 'skip_continue' proceeds to next microtask",
    )
    security_failure_always_stops: bool = Field(
        default=True,
        description="When True, security review failures always stop the workflow regardless of on_failure setting",
    )
    enable_llm_review_grounding: bool = Field(
        default=True,
        description=(
            "Forwarded to the code-review gate's llm_review_fn for call-signature "
            "compatibility, but both V2 teams' coordinator-backed LLM fallback "
            "(_run_llm_review) treat it as a no-op: the coordinator's chunk "
            "reviewer only ever reports on the literal code it was shown, so "
            "there is no free-text hallucinated-claim filter left to toggle"
        ),
    )
    grounding_failure_cycle_limit: int = Field(
        default=3,
        description=(
            "Consecutive code-review cycles with grounding-heavy rejection before "
            "the circuit breaker trips and the microtask fails fast"
        ),
    )
    grounding_failure_ratio_threshold: float = Field(
        default=0.75,
        description=(
            "Minimum fraction of raw LLM issues dropped by grounding in a failed "
            "code-review call to count as a grounding-heavy rejection"
        ),
    )


class MicrotaskReviewFailedError(Exception):
    """Raised when a microtask fails review and on_failure='stop'."""

    def __init__(self, microtask: "Any", review_result: "ReviewResult") -> None:
        self.microtask = microtask
        self.review_result = review_result
        super().__init__(f"Microtask {microtask.id} failed review after max retries")
