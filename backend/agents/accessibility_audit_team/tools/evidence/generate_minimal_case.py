"""
Tool: repro.generate_minimal_case

Isolate minimal repro snippet (web).
"""


from pydantic import BaseModel, Field


class GenerateMinimalCaseInput(BaseModel):
    """Input for generating a minimal reproduction case."""

    audit_id: str = Field(..., description="Audit identifier")
    finding_id: str = Field(..., description="Finding to reproduce")
    dom_context_ref: str = Field(
        default="", description="Reference to DOM context snapshot"
    )
    goal: str = Field(
        default="",
        description="What the minimal repro should demonstrate",
    )
    include_styles: bool = Field(
        default=True, description="Include relevant CSS"
    )
    include_scripts: bool = Field(
        default=False, description="Include relevant JavaScript"
    )


class GenerateMinimalCaseOutput(BaseModel):
    """Output from generating a minimal reproduction case."""

    snippet_ref: str = Field(
        default="", description="Reference to the saved snippet"
    )
    html_snippet: str = Field(
        default="", description="HTML code for the minimal repro"
    )
    css_snippet: str = Field(default="", description="CSS code if included")
    js_snippet: str = Field(default="", description="JS code if included")
    notes: str = Field(default="")
    confidence: float = Field(
        default=0.0, ge=0.0, le=1.0,
        description="Confidence that this minimal repro captures the issue",
    )
    requires_context: bool = Field(
        default=False,
        description="True if repro requires additional context to work",
    )


async def generate_minimal_case(
    input_data: GenerateMinimalCaseInput,
) -> GenerateMinimalCaseOutput:
    """
    Generate a minimal reproduction case for a web accessibility issue.

    A minimal repro:
    - Contains only the code necessary to demonstrate the issue
    - Can be dropped into a test page to verify the bug
    - Helps developers understand the fix required
    - Enables regression testing

    Particularly useful for systemic bugs that appear in design system
    components.

    Used by Reproduction & Evidence Engineer (REE).
    """
    snippet_ref = f"repro_{input_data.audit_id}_{input_data.finding_id}"

    return GenerateMinimalCaseOutput(
        snippet_ref=snippet_ref,
        html_snippet="",
        css_snippet="",
        js_snippet="",
        notes="",
        confidence=0.0,
        requires_context=False,
    )
