"""Models for the QA Expert agent."""

from typing import List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from software_engineering_team.shared.models import SystemArchitecture


class BugReport(BaseModel):
    """A bug or quality issue identified during QA review."""

    severity: str  # critical, high, medium, low
    description: str
    location: str = ""
    # ``file_path`` and ``line_or_section`` are populated by the LLM in
    # ``fix_build`` mode where a build failure points at a specific file/line.
    # When present and ``location`` is empty they are collapsed into
    # ``location`` by the validator below; existing callers that construct
    # ``BugReport(location=...)`` directly are unaffected.
    file_path: str = ""
    line_or_section: str = ""
    steps_to_reproduce: str = ""
    expected_vs_actual: str = ""
    recommendation: str = Field(
        default="",
        description="Concrete recommendation for the coding agent: what to implement to fix this issue.",
    )

    @model_validator(mode="after")
    def _collapse_location(self) -> "BugReport":
        if not self.location and self.file_path:
            if self.line_or_section:
                self.location = f"{self.file_path}:{self.line_or_section}"
            else:
                self.location = self.file_path
        return self


class QAInput(BaseModel):
    """Input for the QA Expert agent."""

    code: str
    language: str = "python"
    task_description: str = ""
    architecture: Optional[SystemArchitecture] = None
    run_instructions: Optional[str] = None
    build_errors: Optional[str] = Field(
        default=None,
        description="Compiler/build or syntax error output when code failed to build.",
    )
    request_mode: Optional[str] = Field(
        default=None,
        description="Mode: 'fix_build' (analyze build errors, produce fix recommendations), "
        "'write_tests' (produce unit_tests and integration_tests), or None (general bug review).",
    )


class QAOutput(BaseModel):
    """Output from the QA Expert agent."""

    bugs_found: List[BugReport] = Field(
        default_factory=list,
        description="List of QA issues for the coding agent to fix. Coding agent implements fixes.",
    )
    approved: bool = Field(
        default=True,
        description="True when code passes review (no critical/high bugs). Merge when approved.",
    )
    integration_tests: str = Field(
        default="", description="Integration test code (for QA-only tasks)"
    )
    unit_tests: str = Field(default="", description="Unit tests for 85%+ coverage")
    test_plan: str = ""
    summary: str = ""
    live_test_notes: str = Field(default="", description="Notes from running the application")
    readme_content: str = Field(
        default="", description="README.md content for build, run, test, deploy"
    )
    suggested_commit_message: str = Field(
        default="",
        description="Conventional Commits format, e.g. test: add integration tests for auth",
    )

    @field_validator("integration_tests", "unit_tests", "readme_content", mode="after")
    @classmethod
    def _unescape_literal_newlines(cls, v: str) -> str:
        """Some LLMs emit escaped ``\\n`` sequences inside long string fields.

        Upstream code used to normalize these in the agent after parsing; the
        behavior now lives on the model so every caller — Strands or legacy
        — gets the same cleanup.
        """
        if v and "\\n" in v:
            return v.replace("\\n", "\n")
        return v
