"""Models for the blog plan critic (independent plan evaluator)."""

from __future__ import annotations

from typing import List, Optional

from pydantic import Field
from shared.gate_report import GateReport, GateSeverity, GateStatus, GateViolation


class PlanViolation(GateViolation):
    """A single rubric or brand-spec violation found in the plan."""

    severity: GateSeverity = Field(
        default="must_fix",
        description="must_fix blocks critic approval; should_fix and consider are advisory.",
    )
    section: Optional[str] = Field(
        default=None,
        description="Section title this violation applies to, or 'overall' for plan-wide issues.",
    )
    evidence_quote: Optional[str] = Field(
        default=None,
        description="Exact quote from the offending plan field (keep under ~120 chars).",
    )
    suggested_fix: str = Field(
        ...,
        description="Concrete, actionable instruction the refiner can apply next iteration.",
    )


class PlanCriticReport(GateReport):
    """Structured critique of a ContentPlan against the author's brand spec and the rubric."""

    status: GateStatus = Field(
        ...,
        description="PASS when no must_fix violations exist; FAIL otherwise.",
    )
    approved: bool = Field(
        ...,
        description=(
            "True when the critic considers the plan shippable to the draft phase. "
            "Should equal (status == 'PASS')."
        ),
    )
    violations: List[PlanViolation] = Field(default_factory=list)
    notes: Optional[str] = Field(
        default=None,
        description="Optional short critic notes, e.g. parse-failure hints or context.",
    )
    rubric_version: str = Field(default="v1")

    def must_fix_count(self) -> int:
        return sum(1 for v in self.violations if v.severity == "must_fix")
