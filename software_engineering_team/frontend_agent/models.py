"""Models for the Frontend Expert agent."""

from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from shared.models import SystemArchitecture


class FrontendInput(BaseModel):
    """Input for the Frontend Expert agent."""

    task_description: str
    requirements: str = ""
    architecture: Optional[SystemArchitecture] = None
    existing_code: Optional[str] = None
    api_endpoints: Optional[str] = None


class FrontendOutput(BaseModel):
    """Output from the Frontend Expert agent."""

    code: str = ""
    summary: str = ""
    files: Dict[str, str] = Field(default_factory=dict)
    components: List[str] = Field(default_factory=list)
    suggested_commit_message: str = Field(
        default="",
        description="Conventional Commits format, e.g. feat(ui): add login component",
    )
