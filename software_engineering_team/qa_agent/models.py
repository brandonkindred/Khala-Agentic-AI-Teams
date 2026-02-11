"""Models for the QA Expert agent."""

from typing import List, Optional

from pydantic import BaseModel, Field

from shared.models import SystemArchitecture


class BugReport(BaseModel):
    """A bug identified during testing."""

    severity: str  # critical, high, medium, low
    description: str
    location: str = ""
    steps_to_reproduce: str = ""
    expected_vs_actual: str = ""


class QAInput(BaseModel):
    """Input for the QA Expert agent."""

    code: str
    language: str = "python"
    task_description: str = ""
    architecture: Optional[SystemArchitecture] = None
    run_instructions: Optional[str] = None


class QAOutput(BaseModel):
    """Output from the QA Expert agent."""

    bugs_found: List[BugReport] = Field(default_factory=list)
    fixed_code: str = Field(default="", description="Code with bug fixes applied")
    integration_tests: str = Field(default="", description="Integration test code")
    unit_tests: str = Field(default="", description="Unit tests for 85%+ coverage")
    test_plan: str = ""
    summary: str = ""
    live_test_notes: str = Field(default="", description="Notes from running the application")
    readme_content: str = Field(default="", description="README.md content for build, run, test, deploy")
    suggested_commit_message: str = Field(
        default="",
        description="Conventional Commits format, e.g. test: add integration tests for auth",
    )
