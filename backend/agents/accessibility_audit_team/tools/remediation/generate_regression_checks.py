"""
Tool: tests.generate_regression_checks

Generate regression test ideas and optional scripts.
"""

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class RegressionTest(BaseModel):
    """A single regression test definition."""

    name: str = Field(..., description="Test name")
    test_type: Literal["automated", "manual"] = Field(default="automated")
    steps: List[str] = Field(default_factory=list)
    script_ref: str = Field(
        default="", description="Reference to test script file"
    )
    script_content: str = Field(
        default="", description="Inline test script if brief"
    )


class GenerateRegressionChecksInput(BaseModel):
    """Input for generating regression checks."""

    audit_id: str = Field(..., description="Audit identifier")
    component: str = Field(..., description="Component to test")
    issue_types: List[str] = Field(
        default_factory=list, description="Issue types to cover"
    )
    preferred_runner: Literal["playwright", "cypress", "detox", "other"] = Field(
        default="playwright"
    )
    generate_scripts: bool = Field(
        default=True, description="Generate executable test scripts"
    )


class GenerateRegressionChecksOutput(BaseModel):
    """Output from generating regression checks."""

    tests: List[RegressionTest] = Field(default_factory=list)
    total_tests: int = Field(default=0)
    automated_tests: int = Field(default=0)
    manual_tests: int = Field(default=0)


# Test templates by issue type
TEST_TEMPLATES = {
    "keyboard": [
        RegressionTest(
            name="keyboard_focusable",
            test_type="automated",
            steps=[
                "Focus the component using Tab",
                "Verify focus is received",
                "Verify focus indicator is visible",
            ],
        ),
        RegressionTest(
            name="keyboard_activatable",
            test_type="automated",
            steps=[
                "Focus the component",
                "Press Enter or Space",
                "Verify component is activated",
            ],
        ),
    ],
    "focus": [
        RegressionTest(
            name="focus_visible",
            test_type="automated",
            steps=[
                "Tab to the component",
                "Verify focus indicator is visible",
                "Verify focus indicator has sufficient contrast",
            ],
        ),
        RegressionTest(
            name="focus_order",
            test_type="automated",
            steps=[
                "Tab through the page",
                "Verify focus order matches visual order",
            ],
        ),
    ],
    "name_role_value": [
        RegressionTest(
            name="accessible_name",
            test_type="automated",
            steps=[
                "Query the component via accessibility tree",
                "Verify accessible name is present and meaningful",
            ],
        ),
        RegressionTest(
            name="correct_role",
            test_type="automated",
            steps=[
                "Query the component via accessibility tree",
                "Verify role matches expected semantic",
            ],
        ),
    ],
    "contrast": [
        RegressionTest(
            name="text_contrast",
            test_type="automated",
            steps=[
                "Get computed foreground and background colors",
                "Calculate contrast ratio",
                "Verify ratio meets 4.5:1 (normal) or 3:1 (large)",
            ],
        ),
    ],
}


async def generate_regression_checks(
    input_data: GenerateRegressionChecksInput,
) -> GenerateRegressionChecksOutput:
    """
    Generate regression test definitions and optional scripts.

    Creates tests that:
    - Prevent the fixed accessibility issue from recurring
    - Can be integrated into CI/CD pipelines
    - Cover both automated and manual verification

    Used by Remediation Advisor (RA) and shared with development teams.
    """
    tests = []

    for issue_type in input_data.issue_types:
        normalized = issue_type.lower().replace("-", "_").replace(" ", "_")
        templates = TEST_TEMPLATES.get(normalized, [])

        for template in templates:
            test = RegressionTest(
                name=f"{input_data.component}_{template.name}",
                test_type=template.test_type,
                steps=template.steps,
                script_ref="",
                script_content="",
            )
            tests.append(test)

    automated = sum(1 for t in tests if t.test_type == "automated")
    manual = sum(1 for t in tests if t.test_type == "manual")

    return GenerateRegressionChecksOutput(
        tests=tests,
        total_tests=len(tests),
        automated_tests=automated,
        manual_tests=manual,
    )
