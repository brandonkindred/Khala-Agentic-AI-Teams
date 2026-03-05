"""
Tool: qa.validate_finding

Validate completeness and consistency of a finding.
"""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from ...models import Finding, Severity, Scope


class ValidationIssue(BaseModel):
    """A single validation issue."""

    field: str = Field(..., description="Field with the issue")
    problem: str = Field(..., description="Description of the problem")
    required_fix: str = Field(..., description="What needs to be done")
    severity: Literal["error", "warning"] = Field(default="error")


class ValidateFindingInput(BaseModel):
    """Input for validating a finding."""

    audit_id: str = Field(..., description="Audit identifier")
    finding: Finding
    ruleset: Literal["strict", "standard"] = Field(
        default="strict",
        description="Validation ruleset to apply",
    )


class ValidateFindingOutput(BaseModel):
    """Output from finding validation."""

    passed: bool = Field(default=False)
    issues: List[ValidationIssue] = Field(default_factory=list)
    normalized_severity: Optional[Severity] = Field(
        default=None, description="Normalized severity after review"
    )
    normalized_scope: Optional[Scope] = Field(
        default=None, description="Normalized scope after review"
    )
    ready_for_report: bool = Field(default=False)


async def validate_finding(
    input_data: ValidateFindingInput,
) -> ValidateFindingOutput:
    """
    Validate finding completeness and consistency.

    Enforces the quality bar:
    - Repro steps present and actionable
    - Expected vs actual documented
    - User impact statement present
    - Evidence artifacts present
    - Standards mapping with confidence
    - Remediation + acceptance criteria + test plan

    Findings without evidence or repro steps are NOT reportable.

    Used by QA & Consistency Reviewer (QCR).
    """
    finding = input_data.finding
    issues = []

    # Check required fields for reportable finding
    if not finding.repro_steps:
        issues.append(
            ValidationIssue(
                field="repro_steps",
                problem="Missing reproduction steps",
                required_fix="Add step-by-step repro instructions",
                severity="error",
            )
        )

    if not finding.expected:
        issues.append(
            ValidationIssue(
                field="expected",
                problem="Missing expected behavior",
                required_fix="Document what should happen",
                severity="error",
            )
        )

    if not finding.actual:
        issues.append(
            ValidationIssue(
                field="actual",
                problem="Missing actual behavior",
                required_fix="Document what actually happens",
                severity="error",
            )
        )

    if not finding.user_impact:
        issues.append(
            ValidationIssue(
                field="user_impact",
                problem="Missing user impact statement",
                required_fix="Explain who is harmed and how",
                severity="error",
            )
        )

    if not finding.evidence_pack_ref:
        issues.append(
            ValidationIssue(
                field="evidence_pack_ref",
                problem="No evidence attached",
                required_fix="Attach evidence pack with screenshots/recordings",
                severity="error",
            )
        )

    if not finding.wcag_mappings:
        issues.append(
            ValidationIssue(
                field="wcag_mappings",
                problem="No WCAG mappings",
                required_fix="Map to appropriate WCAG success criteria",
                severity="error",
            )
        )

    if not finding.acceptance_criteria:
        issues.append(
            ValidationIssue(
                field="acceptance_criteria",
                problem="No acceptance criteria",
                required_fix="Add testable acceptance criteria",
                severity="error",
            )
        )

    # Warnings (not blocking)
    if not finding.recommended_fix:
        issues.append(
            ValidationIssue(
                field="recommended_fix",
                problem="No fix recommendation",
                required_fix="Add remediation guidance",
                severity="warning",
            )
        )

    if not finding.test_plan:
        issues.append(
            ValidationIssue(
                field="test_plan",
                problem="No test plan",
                required_fix="Add verification test steps",
                severity="warning",
            )
        )

    # Check confidence threshold
    if finding.confidence < 0.6:
        issues.append(
            ValidationIssue(
                field="confidence",
                problem=f"Low confidence ({finding.confidence})",
                required_fix="Get AT verification to increase confidence",
                severity="warning",
            )
        )

    # Determine if passed (no errors)
    errors = [i for i in issues if i.severity == "error"]
    passed = len(errors) == 0
    ready_for_report = passed and finding.confidence >= 0.6

    return ValidateFindingOutput(
        passed=passed,
        issues=issues,
        normalized_severity=finding.severity,
        normalized_scope=finding.scope,
        ready_for_report=ready_for_report,
    )
