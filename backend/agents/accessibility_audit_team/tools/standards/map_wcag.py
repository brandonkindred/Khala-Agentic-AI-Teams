"""
Tool: standards.map_wcag

Suggest WCAG SC mappings with confidence and rationale.
"""

from typing import List, Literal

from pydantic import BaseModel, Field

from ...wcag_criteria import get_criterion


class FindingInfo(BaseModel):
    """Finding information for WCAG mapping."""

    title: str = Field(..., description="Finding title")
    summary: str = Field(default="", description="Finding summary")
    expected: str = Field(default="", description="Expected behavior")
    actual: str = Field(default="", description="Actual behavior")
    issue_type: str = Field(default="", description="Issue type classification")
    surface: Literal["web", "ios", "android", "pdf"] = Field(default="web")


class WCAGCandidate(BaseModel):
    """A candidate WCAG success criterion mapping."""

    sc: str = Field(..., description="Success criterion number")
    name: str = Field(default="")
    level: str = Field(default="")
    confidence: float = Field(..., ge=0.0, le=1.0)
    rationale: str = Field(default="", description="Why this SC applies")


class MapWcagInput(BaseModel):
    """Input for mapping a finding to WCAG success criteria."""

    audit_id: str = Field(..., description="Audit identifier")
    finding: FindingInfo


class MapWcagOutput(BaseModel):
    """Output from WCAG mapping."""

    candidates: List[WCAGCandidate] = Field(default_factory=list)
    recommended: List[str] = Field(
        default_factory=list, description="Recommended SC numbers"
    )
    needs_verification: bool = Field(
        default=False,
        description="True if confidence < 0.6 for all candidates",
    )


# Issue type to WCAG SC mapping heuristics
ISSUE_TYPE_SC_MAP = {
    "name_role_value": ["4.1.2", "1.3.1"],
    "keyboard": ["2.1.1", "2.1.2", "2.1.4"],
    "focus": ["2.4.3", "2.4.7", "2.4.11", "2.4.13"],
    "forms": ["3.3.1", "3.3.2", "3.3.3", "3.3.4", "1.3.5"],
    "contrast": ["1.4.3", "1.4.11"],
    "structure": ["1.3.1", "1.3.2", "2.4.1", "2.4.6"],
    "timing": ["2.2.1", "2.2.2"],
    "media": ["1.2.1", "1.2.2", "1.2.3", "1.2.4", "1.2.5", "1.4.2"],
    "motion": ["2.3.1"],
    "input_modality": ["2.5.1", "2.5.2", "2.5.3", "2.5.4"],
    "error_handling": ["3.3.1", "3.3.3", "3.3.4"],
    "navigation": ["2.4.1", "2.4.2", "2.4.4", "2.4.5"],
    "resizing_reflow": ["1.4.4", "1.4.10", "1.4.12"],
    "gestures_dragging": ["2.5.1", "2.5.7"],
    "target_size": ["2.5.8"],
}


async def map_wcag(input_data: MapWcagInput) -> MapWcagOutput:
    """
    Map a finding to WCAG 2.2 success criteria with confidence scores.

    Uses issue type and finding details to suggest appropriate SC mappings.
    Each mapping includes a confidence score and rationale.

    Used by Standards & Legal Mapping Specialist (SLMS).
    """
    finding = input_data.finding
    candidates = []

    # Get candidate SCs based on issue type
    issue_type = finding.issue_type.lower().replace("-", "_").replace(" ", "_")
    candidate_scs = ISSUE_TYPE_SC_MAP.get(issue_type, [])

    for sc_num in candidate_scs:
        criterion = get_criterion(sc_num)
        if criterion:
            candidates.append(
                WCAGCandidate(
                    sc=sc_num,
                    name=criterion.name,
                    level=criterion.level.value,
                    confidence=0.8,  # Base confidence, would be refined by LLM
                    rationale=f"Issue type '{finding.issue_type}' commonly maps to {sc_num} ({criterion.name})",
                )
            )

    # Sort by confidence
    candidates.sort(key=lambda c: c.confidence, reverse=True)

    # Determine recommended SCs (confidence >= 0.7)
    recommended = [c.sc for c in candidates if c.confidence >= 0.7]

    # Check if needs verification
    needs_verification = all(c.confidence < 0.6 for c in candidates) if candidates else True

    return MapWcagOutput(
        candidates=candidates,
        recommended=recommended,
        needs_verification=needs_verification,
    )
