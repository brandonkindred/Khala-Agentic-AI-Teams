"""
Models for the blog compliance agent (Brand and Style Enforcer).
"""

from __future__ import annotations

from typing import List, Optional

from agents.blogging.shared.gate_report import GateReport, GateStatus, GateViolation
from pydantic import Field


class Violation(GateViolation):
    """A single compliance violation."""

    evidence_quotes: List[str] = Field(
        default_factory=list, description="Direct quotes from the draft."
    )
    location_hint: Optional[str] = Field(None, description="Heading name or approximate section.")


class ComplianceReport(GateReport):
    """Output from the Brand and Style Enforcer."""

    status: GateStatus = Field(..., description="PASS or FAIL; FAIL blocks publication.")
    violations: List[Violation] = Field(default_factory=list, description="List of violations.")
    required_fixes: List[str] = Field(
        default_factory=list,
        description="Ordered list of patch instructions for the rewrite agent.",
    )
    notes: Optional[str] = Field(None, description="Optional short notes.")
