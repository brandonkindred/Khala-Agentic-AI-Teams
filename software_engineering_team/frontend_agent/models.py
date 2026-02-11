"""Models for the Frontend Expert agent."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from shared.models import SystemArchitecture


class FrontendInput(BaseModel):
    """Input for the Frontend Expert agent."""

    task_description: str
    requirements: str = ""
    architecture: Optional[SystemArchitecture] = None
    existing_code: Optional[str] = None
    api_endpoints: Optional[str] = None
    qa_issues: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="QA issues to fix. Implement fixes and commit to feature branch.",
    )
    security_issues: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Security issues to fix. Implement fixes and commit to feature branch.",
    )


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
