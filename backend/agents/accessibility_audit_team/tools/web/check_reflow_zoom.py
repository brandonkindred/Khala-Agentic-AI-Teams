"""
Tool: web.check_reflow_zoom

Validate layout behavior at 320 CSS px and zoom levels.
"""

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class ReflowMode(BaseModel):
    """A reflow/zoom test mode."""

    type: Literal["viewport", "zoom"] = Field(
        ..., description="Test type: viewport or zoom"
    )
    width: Optional[int] = Field(default=None, description="Viewport width for type=viewport")
    height: Optional[int] = Field(default=None, description="Viewport height")
    percent: Optional[int] = Field(default=None, description="Zoom percentage for type=zoom")


class ReflowIssue(BaseModel):
    """A detected reflow/zoom issue."""

    mode: str = Field(..., description="Test mode that triggered the issue")
    description: str = Field(default="")
    screenshot_ref: str = Field(default="")
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    affected_elements: List[str] = Field(
        default_factory=list, description="CSS selectors of affected elements"
    )
    issue_type: Literal[
        "horizontal_scroll",
        "content_overlap",
        "content_clipped",
        "text_overflow",
        "interactive_element_inaccessible",
    ] = Field(default="horizontal_scroll")


class CheckReflowZoomInput(BaseModel):
    """Input for checking reflow and zoom behavior."""

    audit_id: str = Field(..., description="Audit identifier")
    url: str = Field(..., description="URL to test")
    modes: List[ReflowMode] = Field(
        default_factory=lambda: [
            ReflowMode(type="viewport", width=320, height=800),
            ReflowMode(type="zoom", percent=200),
            ReflowMode(type="zoom", percent=400),
        ],
        description="Test modes to run",
    )
    capture: Dict[str, bool] = Field(
        default_factory=lambda: {"screenshots": True}
    )
    check_horizontal_scroll: bool = Field(
        default=True, description="Check for horizontal scrolling at 320px"
    )


class CheckReflowZoomOutput(BaseModel):
    """Output from reflow/zoom check."""

    url: str
    issues: List[ReflowIssue] = Field(default_factory=list)
    modes_tested: List[str] = Field(default_factory=list)
    passes_reflow: bool = Field(
        default=True, description="Whether page passes WCAG 1.4.10 Reflow"
    )
    passes_zoom: bool = Field(
        default=True, description="Whether page passes WCAG 1.4.4 Resize Text"
    )
    screenshots: Dict[str, str] = Field(
        default_factory=dict, description="Mode -> screenshot ref"
    )


async def check_reflow_zoom(
    input_data: CheckReflowZoomInput,
) -> CheckReflowZoomOutput:
    """
    Check page layout behavior at 320 CSS px width and zoom levels.

    Tests for:
    - WCAG 1.4.10 Reflow: Content at 320px without horizontal scroll
    - WCAG 1.4.4 Resize Text: Content usable at 200% zoom

    Used by Web Audit Specialist (WAS) to verify responsive accessibility.
    """
    # Schema definition - actual implementation will use browser automation
    modes_tested = []
    for mode in input_data.modes:
        if mode.type == "viewport":
            modes_tested.append(f"viewport_{mode.width}x{mode.height}")
        else:
            modes_tested.append(f"zoom_{mode.percent}%")

    return CheckReflowZoomOutput(
        url=input_data.url,
        issues=[],
        modes_tested=modes_tested,
        passes_reflow=True,
        passes_zoom=True,
        screenshots={},
    )
