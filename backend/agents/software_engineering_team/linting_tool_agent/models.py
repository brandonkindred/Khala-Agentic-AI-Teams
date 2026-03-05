"""Models for the Linting Tool Agent."""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from build_fix_specialist.models import CodeEdit


class LintIssue(BaseModel):
    """A single lint violation reported by a linter."""

    file_path: str = Field(description="Relative path to the file with the violation")
    line: int = Field(description="1-based line number")
    column: int = Field(default=0, description="1-based column number (0 if unknown)")
    rule: str = Field(default="", description="Linter rule code, e.g. 'E501', 'no-unused-vars'")
    message: str = Field(description="Human-readable description of the violation")
    severity: Literal["error", "warning", "info"] = Field(
        default="warning",
        description="Severity classification of the lint issue",
    )


class LintPlan(BaseModel):
    """Output of the planning phase: which linter to run and on what scope."""

    linter_name: str = Field(description="Linter identifier: 'ruff', 'flake8', 'ng_lint', 'eslint'")
    linter_command: List[str] = Field(description="Command to execute, e.g. ['ruff', 'check', '.']")
    config_file: Optional[str] = Field(
        default=None,
        description="Path to the linter config file if detected (e.g. 'pyproject.toml')",
    )
    scope_paths: List[str] = Field(
        default_factory=lambda: ["."],
        description="Paths the linter will check",
    )


class LintExecutionResult(BaseModel):
    """Output of the execution phase: linter subprocess results."""

    success: bool = Field(description="True when the linter reports zero violations")
    issues: List[LintIssue] = Field(default_factory=list, description="Parsed lint violations")
    raw_output: str = Field(default="", description="Combined stdout+stderr from the linter")
    issue_count: int = Field(default=0, description="Total number of issues found")


class LintToolInput(BaseModel):
    """Input payload for the Linting Tool Agent."""

    repo_path: str = Field(description="Absolute path to the project repository")
    agent_type: Literal["backend", "frontend"] = Field(
        description="Which stack is being linted",
    )
    task_id: str = Field(default="", description="Current task identifier (for logging)")
    task_description: str = Field(default="", description="Brief task context")


class LintToolOutput(BaseModel):
    """Full output from the Linting Tool Agent across all three phases."""

    plan: LintPlan = Field(description="Planning phase output")
    execution_result: LintExecutionResult = Field(description="Execution phase output")
    edits: List[CodeEdit] = Field(
        default_factory=list,
        description="Review phase: concrete code edits to fix lint violations",
    )
    linter_issues: List[LintIssue] = Field(
        default_factory=list,
        description="All lint issues found (for fallback when edits cannot be produced)",
    )
    summary: str = Field(default="", description="Brief human-readable summary of the lint run")
