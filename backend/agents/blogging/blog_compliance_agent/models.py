"""
Models for the blog compliance agent (Brand and Style Enforcer).
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

Status = Literal["PASS", "FAIL"]


class Violation(BaseModel):
    """A single compliance violation."""

    rule_id: str = Field(..., description="Rule identifier (e.g. formatting.min_paragraph_sentences).")
    description: str = Field(..., description="Description of the violation.")
    evidence_quotes: List[str] = Field(default_factory=list, description="Direct quotes from the draft.")
    location_hint: Optional[str] = Field(None, description="Heading name or approximate section.")


class ComplianceReport(BaseModel):
    """Output from the Brand and Style Enforcer."""

    status: Status = Field(..., description="PASS or FAIL; FAIL blocks publication.")
    violations: List[Violation] = Field(default_factory=list, description="List of violations.")
    required_fixes: List[str] = Field(
        default_factory=list,
        description="Ordered list of patch instructions for the rewrite agent.",
    )
    notes: Optional[str] = Field(None, description="Optional short notes.")

    def to_dict(self) -> Dict[str, Any]:
        """Export for JSON serialization."""
        if hasattr(self, "model_dump"):
            return self.model_dump(exclude_none=True)
        return self.dict(exclude_none=True)
