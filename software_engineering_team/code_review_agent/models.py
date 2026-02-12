"""Models for the Code Review agent."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from shared.models import SystemArchitecture


class CodeReviewIssue(BaseModel):
    """A single issue found during code review."""

    severity: str = Field(
        default="major",
        description="Severity: critical, major, minor, or nit",
    )
    category: str = Field(
        default="general",
        description="Category: naming, structure, logic, spec-compliance, standards, integration, testing",
    )
    file_path: str = Field(
        default="",
        description="File path where the issue was found",
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
        description="Primary language: typescript (Angular) or python (FastAPI)",
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
        description="True when code passes review (no critical or major issues). Only approve when code is production-ready.",
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
