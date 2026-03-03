"""Models for the UX Engineer agent."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from software_engineering_team.shared.models import SystemArchitecture


class UXEngineerIssue(BaseModel):
    """A polish/usability issue identified by the UX Engineer."""

    severity: str = "medium"  # critical, major, medium, minor
    category: str = "ux"  # focus, keyboard, usability, motion, feedback
    file_path: str = ""
    description: str = ""
    suggestion: str = Field(
        default="",
        description="Concrete recommendation for the coding agent",
    )


class UXEngineerInput(BaseModel):
    """Input for the UX Engineer agent."""

    code: str
    task_description: str = ""
    task_id: str = ""
    architecture: Optional[SystemArchitecture] = None


class UXEngineerOutput(BaseModel):
    """Output from the UX Engineer agent."""

    issues: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Polish suggestions as code_review-style issues for implementation pass",
    )
    approved: bool = Field(
        default=True,
        description="True when no critical polish issues; can proceed without another pass",
    )
    summary: str = ""
