"""Models for the Code Review agent."""

from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel, Field

from software_engineering_team.shared.models import SystemArchitecture

ReviewProgressCallback = Callable[[str, str, float], None]
"""Progress callback signature: (step, detail, fraction in [0.0, 1.0]).

Steps: ``preparing | reviewing | waiting_retry | parsing | finalizing | done``.

Preconditions (on the callable a caller provides):
    - Must not raise; review progress is observability, never control flow.
    - Must accept (str, str, float) positionally.
"""


def notify_review_progress(
    callback: Optional[ReviewProgressCallback], step: str, detail: str, fraction: float
) -> None:
    """Invoke a review progress callback, or no-op when none is provided.

    Preconditions:
        - ``callback`` is None or satisfies the ReviewProgressCallback contract
          (non-raising; exceptions are deliberately NOT swallowed here so a
          contract violation in the caller surfaces as a bug, not silence).

    Postconditions:
        - When ``callback`` is None, has no effect.
        - Otherwise ``callback(step, detail, fraction)`` was invoked exactly once.
    """
    if callback is not None:
        callback(step, detail, fraction)


def coerce_line(value: Any) -> Optional[int]:
    """Parse an LLM-provided line number into a positive int, or None.

    Postconditions:
        - Returns a positive ``int`` when ``value`` is a valid positive number;
          returns None for None, non-numeric, zero, or negative values (so a bad
          line never becomes an invalid inline-comment anchor).
    """
    if value is None:
        return None
    try:
        line = int(value)
    except (TypeError, ValueError):
        return None
    return line if line > 0 else None


class ChunkReviewInput(BaseModel):
    """Input for reviewing one chunk of code (used by ChunkReviewAgent)."""

    code_chunk: str = Field(
        description="Code to review (one or more files, sized per model context)"
    )
    file_path_or_label: str = Field(
        default="",
        description="File path(s) in this chunk for issue reporting",
    )
    task_description: str = Field(default="", description="Task the coding agent was working on")
    task_requirements: str = Field(default="", description="Detailed task requirements")
    acceptance_criteria: List[str] = Field(
        default_factory=list,
        description="Acceptance criteria the code must meet",
    )
    spec_excerpt: str = Field(default="", description="Spec excerpt (capped ~8K)")
    architecture_overview: str = Field(default="", description="Architecture overview (capped ~2K)")
    existing_codebase_excerpt: Optional[str] = Field(
        default=None,
        description="Existing codebase excerpt (capped ~4K)",
    )


class ChunkReviewOutput(BaseModel):
    """Output from reviewing one chunk (approved, issues, summary for this chunk)."""

    approved: bool = Field(default=False, description="True if chunk has no critical/high issues")
    issues: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Issues found (each with severity, category, file_path, description, suggestion)",
    )
    summary: str = Field(default="", description="Review summary for this chunk")


class CodeReviewIssue(BaseModel):
    """A single issue found during code review."""

    severity: str = Field(
        default="high",
        description="Severity: critical, high, medium, low, or info",
    )
    category: str = Field(
        default="general",
        description="Category: naming, structure, logic, spec-compliance, standards, integration, testing",
    )
    file_path: str = Field(
        default="",
        description="File path where the issue was found",
    )
    line: Optional[int] = Field(
        default=None,
        description="1-based line number in the NEW version of file_path where the issue occurs, "
        "when the issue is tied to a specific line. None for file-wide/structural issues.",
    )
    start_line: Optional[int] = Field(
        default=None,
        description="Optional start line for a multi-line issue; `line` is then the end line.",
    )
    description: str = Field(
        default="",
        description="Clear description of the issue",
    )
    suggestion: str = Field(
        default="",
        description="Concrete suggestion for how to fix the issue",
    )


class CodeReviewInput(BaseModel):
    """Input for the Code Review agent."""

    code: str = Field(
        description="The code to review (all files on the branch, concatenated with file headers)",
    )
    spec_content: str = Field(
        default="",
        description="Full project specification to check code against",
    )
    task_description: str = Field(
        default="",
        description="The task the coding agent was working on",
    )
    task_requirements: str = Field(
        default="",
        description="Detailed requirements for the task",
    )
    acceptance_criteria: List[str] = Field(
        default_factory=list,
        description="Acceptance criteria the code must meet",
    )
    language: str = Field(
        default="typescript",
        description="Primary language: typescript (React/Angular/Vue) or python (FastAPI)",
    )
    architecture: Optional[SystemArchitecture] = None
    existing_codebase: Optional[str] = Field(
        default=None,
        description="Existing code in the repo before the agent's changes",
    )


class CodeReviewOutput(BaseModel):
    """Output from the Code Review agent."""

    approved: bool = Field(
        default=False,
        description="True when code passes review (no critical or high issues). Only approve when code is production-ready.",
    )
    issues: List[CodeReviewIssue] = Field(
        default_factory=list,
        description="List of issues found during code review",
    )
    summary: str = Field(
        default="",
        description="Overall summary of the code review",
    )
    spec_compliance_notes: str = Field(
        default="",
        description="Notes on how well the code meets the specification and acceptance criteria",
    )
    suggested_commit_message: str = Field(
        default="",
        description="Conventional Commits format, if reviewer wants to suggest a better message",
    )
