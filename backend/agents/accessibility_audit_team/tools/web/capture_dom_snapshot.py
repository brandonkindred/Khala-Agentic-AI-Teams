"""
Tool: web.capture_dom_snapshot

DOM + computed style excerpts + a11y tree notes.
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class DomNode(BaseModel):
    """Captured DOM node with accessibility information."""

    selector: str = Field(..., description="CSS selector")
    html: str = Field(default="", description="HTML excerpt")
    tag_name: str = Field(default="")
    computed_styles: Dict[str, str] = Field(
        default_factory=dict, description="Computed CSS styles"
    )
    a11y_node: Optional[Dict[str, Any]] = Field(
        default=None, description="Accessibility tree node info"
    )
    role: str = Field(default="", description="ARIA/implicit role")
    name: str = Field(default="", description="Accessible name")
    description: str = Field(default="", description="Accessible description")
    states: List[str] = Field(
        default_factory=list, description="ARIA states (e.g., expanded, selected)"
    )
    aria_attributes: Dict[str, str] = Field(
        default_factory=dict, description="ARIA attributes"
    )


class CaptureDomSnapshotInput(BaseModel):
    """Input for capturing DOM snapshots."""

    audit_id: str = Field(..., description="Audit identifier")
    url: str = Field(..., description="URL to snapshot")
    selectors: List[str] = Field(
        default_factory=list,
        description="CSS selectors to capture (empty = capture full page)",
    )
    include_computed_styles: bool = Field(
        default=True, description="Include computed CSS styles"
    )
    include_a11y_tree: bool = Field(
        default=True, description="Include accessibility tree info"
    )
    styles_to_capture: List[str] = Field(
        default_factory=lambda: [
            "color",
            "background-color",
            "font-size",
            "font-weight",
            "outline",
            "border",
            "display",
            "visibility",
            "opacity",
        ],
        description="CSS properties to capture",
    )


class CaptureDomSnapshotOutput(BaseModel):
    """Output from DOM snapshot capture."""

    snapshot_ref: str = Field(..., description="Reference to full snapshot")
    nodes: List[DomNode] = Field(default_factory=list)
    page_title: str = Field(default="")
    page_lang: str = Field(default="")
    landmarks: List[Dict[str, str]] = Field(
        default_factory=list, description="Page landmarks"
    )
    headings: List[Dict[str, str]] = Field(
        default_factory=list, description="Heading structure"
    )


async def capture_dom_snapshot(
    input_data: CaptureDomSnapshotInput,
) -> CaptureDomSnapshotOutput:
    """
    Capture DOM snapshot with computed styles and accessibility tree.

    This tool provides the raw data needed for:
    - Structure analysis (headings, landmarks)
    - Name/role/value verification
    - Contrast calculations
    - Focus indicator analysis

    Used by various specialists during Discovery and Verification phases.
    """
    # Schema definition - actual implementation will use browser automation
    return CaptureDomSnapshotOutput(
        snapshot_ref=f"dom_snapshot_{input_data.audit_id}_{hash(input_data.url) % 10000}",
        nodes=[],
        page_title="",
        page_lang="",
        landmarks=[],
        headings=[],
    )
