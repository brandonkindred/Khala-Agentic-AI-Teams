"""
Models for the Fact-Checker and Risk Officer agent.
"""

from __future__ import annotations

from typing import List, Optional

from agents.blogging.shared.gate_report import GateReport, GateStatus
from pydantic import Field


class FactCheckReport(GateReport):
    """Output from the Fact-Checker and Risk Officer."""

    claims_status: GateStatus = Field(
        ..., description="PASS if all claims are supported; FAIL otherwise."
    )
    risk_status: GateStatus = Field(
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
