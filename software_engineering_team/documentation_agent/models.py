"""Models for the Documentation agent."""

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from shared.models import SystemArchitecture


class DocumentationStatus(str, Enum):
    """Progress tracking status for the Documentation agent workflow."""

    STARTING = "starting"
    REVIEWING_CODEBASE = "reviewing_codebase"
    UPDATING_README = "updating_readme"
    CHECKING_CONTRIBUTORS = "checking_contributors"
    COMMITTING = "committing"
    MERGING = "merging"
    COMPLETE = "complete"
    FAILED = "failed"


class DocumentationInput(BaseModel):
    """Input for the Documentation agent."""

    repo_path: str = Field(
        description="Absolute path to the repository root",
    )
    task_id: str = Field(
        description="ID of the task that just completed (used for branch naming)",
    )
    task_summary: str = Field(
        default="",
        description="Summary of the completed task",
    )
    agent_type: str = Field(
        default="",
        description="Type of agent that completed the task (backend, frontend, devops)",
    )
    spec_content: str = Field(
        default="",
        description="Full project specification",
    )
    architecture: Optional[SystemArchitecture] = None
    codebase_content: str = Field(
        default="",
        description="Current codebase content (concatenated files with headers)",
    )
    existing_readme: str = Field(
        default="",
        description="Current content of README.md (empty if none exists)",
    )
    existing_contributors: str = Field(
        default="",
        description="Current content of CONTRIBUTORS.md (empty if none exists)",
    )


class DocumentationOutput(BaseModel):
    """Output from the Documentation agent."""

    readme_content: str = Field(
        default="",
        description="Updated README.md content",
    )
    contributors_content: str = Field(
        default="",
        description="Updated CONTRIBUTORS.md content (empty if no changes needed)",
    )
    readme_changed: bool = Field(
        default=False,
        description="True if README.md was updated",
    )
    contributors_changed: bool = Field(
        default=False,
        description="True if CONTRIBUTORS.md was updated",
    )
    summary: str = Field(
        default="",
        description="Summary of documentation changes made",
    )
    suggested_commit_message: str = Field(
        default="docs(readme): update project documentation",
        description="Conventional Commits format commit message",
    )
