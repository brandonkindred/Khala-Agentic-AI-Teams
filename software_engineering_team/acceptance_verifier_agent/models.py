"""Models for the Acceptance Criteria Verifier agent."""

from typing import List, Optional

from pydantic import BaseModel, Field

from shared.models import SystemArchitecture


class CriterionStatus(BaseModel):
    """Status of a single acceptance criterion."""

    criterion: str
    satisfied: bool
    evidence: str = ""


class AcceptanceVerifierInput(BaseModel):
    """Input for the Acceptance Verifier agent."""

    code: str
    task_description: str
    acceptance_criteria: List[str]
    spec_content: str = ""
    architecture: Optional[SystemArchitecture] = None
    language: str = "python"


class AcceptanceVerifierOutput(BaseModel):
    """Output from the Acceptance Verifier agent."""

    all_satisfied: bool = Field(
        default=True,
        description="True when every acceptance criterion is satisfied.",
    )
    per_criterion: List[CriterionStatus] = Field(
        default_factory=list,
        description="Status and evidence for each criterion.",
    )
    summary: str = ""
