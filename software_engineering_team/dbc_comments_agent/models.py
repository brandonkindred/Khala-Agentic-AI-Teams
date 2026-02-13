"""Models for the Design by Contract Comments agent."""

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from shared.models import SystemArchitecture


class DbcCommentsStatus(str, Enum):
    """Progress tracking status for the DbC Comments agent workflow."""

    STARTING = "starting"
    ANALYZING_CODE = "analyzing_code"
    ADDING_COMMENTS = "adding_comments"
    COMMITTING = "committing"
    COMPLETE = "complete"
    FAILED = "failed"


class DbcCommentsInput(BaseModel):
    """Input for the DbC Comments agent."""

    code: str = Field(
        description="The code to review (all files on the branch, concatenated with file headers)",
    )
    language: str = Field(
        default="python",
        description="Primary language: python, typescript, or java",
    )
    task_description: str = Field(
        default="",
        description="The task the coding agent was working on",
    )
    architecture: Optional[SystemArchitecture] = None


class DbcCommentsOutput(BaseModel):
    """Output from the DbC Comments agent."""

    files: Dict[str, str] = Field(
        default_factory=dict,
        description="Dict of file_path -> updated file content with DbC comments added",
    )
    comments_added: int = Field(
        default=0,
        description="Number of new DbC comments added",
    )
    comments_updated: int = Field(
        default=0,
        description="Number of existing comments updated to comply with DbC",
    )
    already_compliant: bool = Field(
        default=False,
        description="True when all code already has proper DbC comments",
    )
    summary: str = Field(
        default="",
        description="Summary message for the coding agent describing what was changed or praising compliance",
    )
    suggested_commit_message: str = Field(
        default="docs(dbc): add Design by Contract comments",
        description="Conventional Commits format commit message",
    )
