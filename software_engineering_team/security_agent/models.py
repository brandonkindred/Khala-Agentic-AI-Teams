"""Models for the Cybersecurity Expert agent."""

from typing import List, Optional

from pydantic import BaseModel, Field

from shared.models import SystemArchitecture


class SecurityVulnerability(BaseModel):
    """A identified security vulnerability."""

    severity: str  # critical, high, medium, low, info
    category: str  # e.g. injection, xss, auth, crypto
    description: str
    location: str = ""
    recommendation: str = ""


class SecurityInput(BaseModel):
    """Input for the Cybersecurity Expert agent."""

    code: str
    language: str = "python"  # python, java, typescript, etc.
    task_description: str = ""
    architecture: Optional[SystemArchitecture] = None
    context: str = ""


class SecurityOutput(BaseModel):
    """Output from the Cybersecurity Expert agent."""

    vulnerabilities: List[SecurityVulnerability] = Field(default_factory=list)
    fixed_code: str = Field(default="", description="Code with security fixes applied")
    summary: str = ""
    remediations: List[dict] = Field(default_factory=list)
