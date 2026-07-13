"""
Shared primitives for the blogging pipeline's PASS/FAIL "gate report" models.

The pipeline runs several independent gates (deterministic validators, brand/style
compliance, fact-check/risk, and plan critic). Each gate emits a report that encodes
the same shape — an overall PASS/FAIL status plus a list of issues — and each issue is
a rule/rubric violation with a description. Historically every gate redefined its own
``Status`` literal and hand-rolled its own ``to_dict``. This module defines those shared
pieces once so the concrete report models can extend them.

Invariants:
    - ``GateStatus`` is the single source of truth for a gate's PASS/FAIL literal.
    - ``GateReport.to_dict`` is the single JSON serializer for every gate report.
"""

from __future__ import annotations

from typing import Any, Dict, Literal

from pydantic import BaseModel, Field

# One canonical PASS/FAIL literal, replacing the per-model redefinitions.
GateStatus = Literal["PASS", "FAIL"]

# One canonical severity literal for critique/feedback items.
GateSeverity = Literal["must_fix", "should_fix", "consider"]


class GateReport(BaseModel):
    """Base for all PASS/FAIL gate reports; one JSON serializer for all of them.

    Concrete reports (validator, compliance, fact-check, plan critic) extend this to
    inherit a single, consistent serialization path. The base intentionally does not
    mandate a single ``status`` field: some reports (e.g. the fact-checker) carry more
    than one status axis.

    Postconditions:
        - ``to_dict()`` returns a plain ``dict`` with all ``None``-valued fields omitted.
    """

    def to_dict(self) -> Dict[str, Any]:
        """Export for JSON serialization, omitting ``None``-valued fields.

        Postconditions:
            - Returns ``self.model_dump(exclude_none=True)``: a plain dict whose keys are
              this model's set fields minus any whose value is ``None``.
        """
        return self.model_dump(exclude_none=True)


class GateViolation(BaseModel):
    """Common base for a single gate violation.

    Holds the fields every gate violation shares. Subclasses add their gate-specific
    fields (evidence, severity, section, suggested fix, ...).

    Preconditions:
        - ``rule_id`` and ``description`` are non-empty strings supplied by the caller.
    """

    rule_id: str = Field(..., description="Rule / rubric identifier.")
    description: str = Field(..., description="What is wrong and why it matters.")
