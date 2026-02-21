"""Models for the Integration / API-contract agent."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from shared.models import SystemArchitecture


class IntegrationIssue(BaseModel):
    """An integration or API contract mismatch."""

    severity: str  # critical, high, medium, low
    category: str  # contract_mismatch, missing_endpoint, wrong_payload, missing_wire_up
    description: str
    backend_location: str = ""
    frontend_location: str = ""
    recommendation: str = ""


class IntegrationInput(BaseModel):
    """Input for the Integration agent."""

    backend_code: str
    frontend_code: str
    spec_content: str = ""
    architecture: Optional[SystemArchitecture] = None


class IntegrationOutput(BaseModel):
    """Output from the Integration agent."""

    passed: bool = Field(
        default=True,
        description="True when no critical/high integration issues found.",
    )
    issues: List[IntegrationIssue] = Field(
        default_factory=list,
        description="List of integration/contract issues.",
    )
    summary: str = ""
    fix_task_suggestions: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Suggested fix tasks (id, title, assignee, description, etc.) for Tech Lead.",
    )
