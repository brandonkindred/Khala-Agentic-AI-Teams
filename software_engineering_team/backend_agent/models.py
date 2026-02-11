"""Models for the Backend Expert agent."""

from typing import Dict, Optional

from pydantic import BaseModel, Field

from shared.models import SystemArchitecture


class BackendInput(BaseModel):
    """Input for the Backend Expert agent."""

    task_description: str
    requirements: str = ""
    language: str = "python"  # python or java
    architecture: Optional[SystemArchitecture] = None
    existing_code: Optional[str] = None
    api_spec: Optional[str] = None


class BackendOutput(BaseModel):
    """Output from the Backend Expert agent."""

    code: str = ""
    language: str = "python"
    summary: str = ""
    files: Dict[str, str] = Field(default_factory=dict)
    tests: str = ""
    suggested_commit_message: str = Field(
        default="",
        description="Conventional Commits format, e.g. feat(api): add user authentication",
    )
