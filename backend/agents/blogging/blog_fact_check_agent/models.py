"""
Models for the Fact-Checker and Risk Officer agent.
"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

Status = Literal["PASS", "FAIL"]


class FactCheckReport(BaseModel):
    """Output from the Fact-Checker and Risk Officer."""

    claims_status: Status = Field(
        ..., description="PASS if all claims are supported; FAIL otherwise."
    )
    risk_status: Status = Field(
        ...,
        description="PASS if no legal/medical/financial/security hazards; FAIL if disclaimers or fixes needed.",
    )
    claims_verified: List[str] = Field(
        default_factory=list, description="Claims that were verified."
    )
    risk_flags: List[str] = Field(
        default_factory=list, description="Legal, medical, financial, or security flags."
    )
    required_disclaimers: List[str] = Field(
        default_factory=list,
        description="Disclaimers to add (e.g. for medical, legal, financial content).",
    )
    notes: Optional[str] = Field(None, description="Optional notes.")
