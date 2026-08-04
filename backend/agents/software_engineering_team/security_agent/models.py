"""Models for the Cybersecurity Expert agent."""

from typing import List, Optional

from pydantic import BaseModel, Field

from software_engineering_team.shared.models import SystemArchitecture


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

    vulnerabilities: List[SecurityVulnerability] = Field(
        default_factory=list,
        description="List of security issues for the coding agent to fix. Coding agent implements fixes.",
    )
    approved: bool = Field(
        default=True,
        description="True when code passes review (no critical/high vulnerabilities). Merge when approved.",
    )
    summary: str = ""
    remediations: List[dict] = Field(default_factory=list)
    suggested_commit_message: str = Field(
        default="",
        description="Conventional Commits format, e.g. fix(security): remediate SQL injection",
    )


class SecurityLLMResponse(BaseModel):
    """Narrow LLM-authored shape for one security-review call's response.

    ``CybersecurityExpertAgent.run`` validates every reply against this model
    via ``llm_service.complete_validated``, replacing the previous Strands
    ``structured_output_model=SecurityOutput`` call (single-shot, no
    corrective retry on a malformed reply).

    All four fields are required, not defaulted: ``SECURITY_PROMPT``'s own
    output-contract reminder explicitly tells the model to always emit
    exactly these four top-level keys, so a reply missing one is a
    truncated/malformed response, not a legitimately empty field. Defaulting
    them here would silently look like a clean, empty-findings result instead
    of failing validation and driving ``complete_validated``'s corrective
    retry.

    There is deliberately no ``approved`` field: the prompt never asks the
    model for one, and ``CybersecurityExpertAgent.run`` always re-derives
    ``SecurityOutput.approved`` from the reported ``vulnerabilities`` via
    :func:`software_engineering_team.shared.security_service.derive_approved`.
    """

    vulnerabilities: List[SecurityVulnerability] = Field(
        description="List of security issues found in the reviewed code."
    )
    summary: str = Field(description="Overall security assessment.")
    remediations: List[dict] = Field(description="Reference list of {issue, recommendation} pairs.")
    suggested_commit_message: str = Field(
        description="Conventional Commits format, e.g. fix(security): remediate SQL injection"
    )
