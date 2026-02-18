"""Models for the Accessibility Expert agent."""

from typing import List, Optional

from pydantic import BaseModel, Field

from shared.models import SystemArchitecture


class AccessibilityIssue(BaseModel):
    """An accessibility issue identified during WCAG 2.2 review."""

    severity: str  # critical, high, medium, low
    wcag_criterion: str = ""  # e.g. "1.1.1", "2.2.1"
    description: str
    location: str = ""
    recommendation: str = Field(
        default="",
        description="Concrete recommendation for the coding agent: what to implement to fix this issue.",
    )


class AccessibilityInput(BaseModel):
    """Input for the Accessibility Expert agent."""

    code: str
    language: str = "typescript"  # typescript, html, etc. for frontend
    task_description: str = ""
    architecture: Optional[SystemArchitecture] = None


class AccessibilityOutput(BaseModel):
    """Output from the Accessibility Expert agent."""

    issues: List[AccessibilityIssue] = Field(
        default_factory=list,
        description="List of WCAG 2.2 accessibility issues for the coding agent to fix. Coding agent implements fixes.",
    )
    approved: bool = Field(
        default=True,
        description="True when code passes review (no critical/high accessibility issues). Merge when approved.",
    )
    summary: str = ""
