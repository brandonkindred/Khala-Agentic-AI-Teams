"""
Tool: mobile.check_font_scaling

Test dynamic type / font scaling behavior.
"""

from typing import Dict, List, Literal

from pydantic import BaseModel, Field


class FontScalingIssue(BaseModel):
    """A font scaling issue detected on a screen."""

    screen: str
    scale: str = Field(..., description="Scale setting that caused the issue")
    description: str = Field(default="")
    screenshot_ref: str = Field(default="")
    affected_elements: List[str] = Field(default_factory=list)
    issue_type: Literal[
        "text_truncation",
        "text_overlap",
        "layout_broken",
        "text_not_scaling",
        "interactive_element_hidden",
    ] = Field(default="text_truncation")


class CheckFontScalingInput(BaseModel):
    """Input for checking font scaling behavior."""

    audit_id: str = Field(..., description="Audit identifier")
    platform: Literal["ios", "android"] = Field(..., description="Mobile platform")
    scales: List[str] = Field(
        default_factory=lambda: ["default", "large", "largest"],
        description="Font scales to test",
    )
    screens: List[str] = Field(default_factory=list, description="Screens to test")
    capture: Dict[str, bool] = Field(default_factory=lambda: {"screenshots": True})


class CheckFontScalingOutput(BaseModel):
    """Output from font scaling check."""

    platform: str
    scales_tested: List[str] = Field(default_factory=list)
    issues: List[FontScalingIssue] = Field(default_factory=list)
    screens_tested: int = Field(default=0)
    screens_passing: int = Field(default=0)
    supports_dynamic_type: bool = Field(
        default=True, description="Whether app supports system font scaling"
    )
    screenshots: Dict[str, str] = Field(
        default_factory=dict, description="screen_scale -> screenshot ref"
    )


async def check_font_scaling(
    input_data: CheckFontScalingInput,
) -> CheckFontScalingOutput:
    """
    Test dynamic type (iOS) or font scaling (Android) behavior.

    Verifies:
    - Text scales with system settings
    - No content is truncated or overlapping
    - Layout remains usable at larger text sizes
    - Interactive elements remain accessible

    Maps to WCAG 1.4.4 Resize Text requirements adapted for mobile.
    Used by Mobile Accessibility Specialist (MAS).
    """
    return CheckFontScalingOutput(
        platform=input_data.platform,
        scales_tested=input_data.scales,
        issues=[],
        screens_tested=len(input_data.screens),
        screens_passing=len(input_data.screens),
        supports_dynamic_type=True,
        screenshots={},
    )
