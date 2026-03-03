"""Models for the Documentation agent."""

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from software_engineering_team.shared.models import SystemArchitecture


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
        description="Current content of root README.md (empty if none exists)",
    )
    existing_readme_frontend: str = Field(
        default="",
        description="Current content of frontend/README.md (empty if none exists)",
    )
    existing_readme_backend: str = Field(
        default="",
        description="Current content of backend/README.md (empty if none exists)",
    )
    existing_readme_devops: str = Field(
        default="",
        description="Current content of devops/README.md (empty if none exists)",
    )
    existing_contributors: str = Field(
        default="",
        description="Current content of CONTRIBUTORS.md (empty if none exists)",
    )
    has_frontend_folder: bool = Field(
        default=True,
        description="Whether the repo has a frontend/ folder (write frontend/README.md only if true)",
    )
    has_backend_folder: bool = Field(
        default=True,
        description="Whether the repo has a backend/ folder (write backend/README.md only if true)",
    )
    has_devops_folder: bool = Field(
        default=True,
        description="Whether the repo has a devops/ folder (write devops/README.md only if true)",
    )
    is_final_review: bool = Field(
        default=False,
        description="If true, use final comprehensive review mode (emphasize completeness)",
    )
    completed_task_ids: Optional[List[str]] = Field(
        default=None,
        description="List of completed task IDs for CONTRIBUTORS (final review only)",
    )


class DocumentationOutput(BaseModel):
    """Output from the Documentation agent."""

    readme_content: str = Field(
        default="",
        description="Updated root README.md content",
    )
    readme_frontend_content: str = Field(
        default="",
        description="Updated frontend/README.md content",
    )
    readme_backend_content: str = Field(
        default="",
        description="Updated backend/README.md content",
    )
    readme_devops_content: str = Field(
        default="",
        description="Updated devops/README.md content",
    )
    contributors_content: str = Field(
        default="",
        description="Updated CONTRIBUTORS.md content (empty if no changes needed)",
    )
    readme_changed: bool = Field(
        default=False,
        description="True if root README.md was updated",
    )
    readme_frontend_changed: bool = Field(
        default=False,
        description="True if frontend/README.md was updated",
    )
    readme_backend_changed: bool = Field(
        default=False,
        description="True if backend/README.md was updated",
    )
    readme_devops_changed: bool = Field(
        default=False,
        description="True if devops/README.md was updated",
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
