"""
Tool: remediation.suggest_fix

Generate fix recipe + acceptance criteria + test plan.
"""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class FindingInput(BaseModel):
    """Finding information for remediation."""

    issue_type: str
    surface: Literal["web", "ios", "android"]
    component: str = Field(default="", description="Component or pattern name")
    summary: str
    evidence_pack_ref: str = Field(default="")


class StackInfo(BaseModel):
    """Tech stack information."""

    web: Literal["react", "angular", "vue", "other"] = Field(default="other")
    mobile: Literal["native", "rn", "flutter", "other"] = Field(default="other")


class SuggestFixInput(BaseModel):
    """Input for suggesting a fix."""

    audit_id: str = Field(..., description="Audit identifier")
    finding: FindingInput
    stack: StackInfo = Field(default_factory=StackInfo)


class SuggestFixOutput(BaseModel):
    """Output from fix suggestion."""

    root_cause_hypothesis: str = Field(default="")
    recommended_fix: List[str] = Field(
        default_factory=list, description="Step-by-step fix recipe"
    )
    acceptance_criteria: List[str] = Field(
        default_factory=list, description="Testable acceptance criteria"
    )
    test_plan: List[str] = Field(
        default_factory=list, description="Verification test steps"
    )
    code_examples_ref: str = Field(
        default="", description="Reference to code examples"
    )
    code_snippet: str = Field(
        default="", description="Inline code example if brief"
    )
    regression_prevention: List[str] = Field(
        default_factory=list, description="Suggestions to prevent regression"
    )


# Common fix patterns by issue type
FIX_PATTERNS = {
    "name_role_value": {
        "root_cause": "Element lacks proper accessible name, role, or value",
        "fix_steps": [
            "Add appropriate ARIA label or visible text",
            "Ensure role is correct (use native element or explicit role)",
            "Expose value/state changes to assistive technology",
        ],
        "acceptance": [
            "Screen reader announces element with correct name, role, and value",
            "Name matches visible text where applicable (WCAG 2.5.3)",
        ],
    },
    "keyboard": {
        "root_cause": "Element not keyboard accessible",
        "fix_steps": [
            "Use native interactive element (button, a, input) or add tabindex='0'",
            "Add keyboard event handlers for Enter/Space",
            "Ensure visual focus indicator is present",
        ],
        "acceptance": [
            "Element is focusable via Tab key",
            "Element is activatable via Enter or Space",
            "Focus indicator is visible",
        ],
    },
    "focus": {
        "root_cause": "Focus management issue",
        "fix_steps": [
            "Ensure logical focus order follows reading order",
            "Add visible focus indicator meeting 3:1 contrast",
            "Manage focus appropriately for dynamic content",
        ],
        "acceptance": [
            "Focus order is logical and predictable",
            "Focus indicator is visible on all interactive elements",
        ],
    },
    "contrast": {
        "root_cause": "Insufficient color contrast",
        "fix_steps": [
            "Adjust foreground or background color to meet 4.5:1 (normal) or 3:1 (large)",
            "For UI components, ensure 3:1 contrast with adjacent colors",
        ],
        "acceptance": [
            "Text contrast ratio meets 4.5:1 (normal) or 3:1 (large)",
            "UI component contrast meets 3:1",
        ],
    },
}


async def suggest_fix(input_data: SuggestFixInput) -> SuggestFixOutput:
    """
    Generate fix recipe, acceptance criteria, and test plan for a finding.

    Provides developer-ready remediation guidance including:
    - Root cause hypothesis
    - Step-by-step fix instructions
    - Testable acceptance criteria
    - Verification test plan
    - Code examples (when applicable)

    Used by Remediation Advisor (RA).
    """
    issue_type = input_data.finding.issue_type.lower().replace("-", "_").replace(" ", "_")
    pattern = FIX_PATTERNS.get(issue_type, {})

    return SuggestFixOutput(
        root_cause_hypothesis=pattern.get("root_cause", f"Issue related to {input_data.finding.issue_type}"),
        recommended_fix=pattern.get("fix_steps", []),
        acceptance_criteria=pattern.get("acceptance", []),
        test_plan=[
            "Verify with keyboard-only navigation",
            "Verify with screen reader (NVDA/VoiceOver)",
            "Run automated accessibility scan",
        ],
        code_examples_ref="",
        code_snippet="",
        regression_prevention=[
            "Add accessibility unit tests",
            "Include in component documentation",
            "Add to design system accessibility contract",
        ],
    )
