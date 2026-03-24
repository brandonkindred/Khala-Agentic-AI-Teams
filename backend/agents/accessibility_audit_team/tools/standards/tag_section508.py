"""
Tool: standards.tag_section508

Apply Section 508 reporting tags to a finding.
"""

from typing import List, Literal

from pydantic import BaseModel, Field

from ...section_508_criteria import get_508_tags_for_wcag_list


class TagSection508Input(BaseModel):
    """Input for applying Section 508 tags."""

    audit_id: str = Field(..., description="Audit identifier")
    wcag_scs: List[str] = Field(..., description="WCAG success criteria numbers")
    surface: Literal["web", "ios", "android", "pdf"] = Field(
        default="web", description="Platform surface"
    )


class TagSection508Output(BaseModel):
    """Output from Section 508 tagging."""

    tags: List[str] = Field(default_factory=list, description="Section 508 section numbers")
    notes: str = Field(default="")
    functional_performance_criteria: List[str] = Field(
        default_factory=list, description="Applicable 302.x FPC"
    )


async def tag_section508(input_data: TagSection508Input) -> TagSection508Output:
    """
    Apply Section 508 reporting tags based on WCAG mappings.

    Uses the WCAG-to-508 crosswalk to identify applicable Section 508
    requirements for compliance reporting purposes.

    Used by Standards & Legal Mapping Specialist (SLMS).
    """
    # Get 508 tags based on WCAG mappings
    tags = get_508_tags_for_wcag_list(input_data.wcag_scs)

    # Determine functional performance criteria
    fpc = []
    for sc in input_data.wcag_scs:
        if sc in ["1.1.1", "1.3.1", "1.3.2", "4.1.2"]:
            fpc.append("302.1")  # Without Vision
        if sc in ["1.4.3", "1.4.4", "1.4.10", "1.4.11"]:
            fpc.append("302.2")  # With Limited Vision
        if sc == "1.4.1":
            fpc.append("302.3")  # Without Perception of Color
        if sc in ["1.2.1", "1.2.2", "1.2.3", "1.2.4", "1.2.5"]:
            fpc.append("302.4")  # Without Hearing
        if sc in ["2.1.1", "2.1.2", "2.5.1", "2.5.2"]:
            fpc.append("302.7")  # With Limited Manipulation

    # Deduplicate FPC
    fpc = sorted(list(set(fpc)))

    notes = ""
    if input_data.surface == "web":
        notes = "Web content: E205.4 requires WCAG 2.0 Level A/AA conformance."
    elif input_data.surface in ["ios", "android"]:
        notes = "Mobile app: Chapter 5 software requirements apply."
    elif input_data.surface == "pdf":
        notes = "PDF: 504.2.2 requires PDF/UA-1 conformance for exported PDFs."

    return TagSection508Output(
        tags=tags,
        notes=notes,
        functional_performance_criteria=fpc,
    )
