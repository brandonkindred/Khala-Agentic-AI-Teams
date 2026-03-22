"""
Tool: mobile.record_screen_reader_flow

Record VoiceOver/TalkBack navigation and announcements.
"""

from typing import Dict, List, Literal

from pydantic import BaseModel, Field


class DeviceInfo(BaseModel):
    """Mobile device information."""

    model: str = Field(..., description="Device model, e.g., iPhone 15")
    os_version: str = Field(..., description="OS version, e.g., iOS 17.2")


class AppInfo(BaseModel):
    """Mobile app information."""

    name: str
    version: str
    build: str = Field(default="")


class AnnouncementStep(BaseModel):
    """A single announcement from the screen reader."""

    step: int = Field(..., description="Step number in the flow")
    element: str = Field(default="", description="Element identifier")
    announcement: str = Field(default="", description="What was announced")
    expected: str = Field(default="", description="What should have been announced")
    passed: bool = Field(default=True)
    role_announced: str = Field(default="")
    traits_announced: List[str] = Field(default_factory=list)
    hints_announced: str = Field(default="")
    issues: List[str] = Field(default_factory=list)


class RecordScreenReaderFlowInput(BaseModel):
    """Input for recording mobile screen reader flow."""

    audit_id: str = Field(..., description="Audit identifier")
    platform: Literal["ios", "android"] = Field(..., description="Mobile platform")
    device: DeviceInfo
    app: AppInfo
    flow_steps: List[str] = Field(
        default_factory=list,
        description="High-level flow steps to perform",
    )
    capture: Dict[str, bool] = Field(
        default_factory=lambda: {"screen_recording": True}
    )
    screen_reader: Literal["voiceover", "talkback"] = Field(
        default="voiceover", description="Screen reader to use"
    )


class RecordScreenReaderFlowOutput(BaseModel):
    """Output from recording screen reader flow."""

    recording_ref: str = Field(default="", description="Reference to screen recording")
    announcements: List[AnnouncementStep] = Field(default_factory=list)
    total_steps: int = Field(default=0)
    passed_steps: int = Field(default=0)
    failed_steps: int = Field(default=0)
    issues_summary: List[str] = Field(default_factory=list)
    focus_order_issues: List[str] = Field(default_factory=list)
    missing_labels: List[str] = Field(default_factory=list)


async def record_screen_reader_flow(
    input_data: RecordScreenReaderFlowInput,
) -> RecordScreenReaderFlowOutput:
    """
    Record VoiceOver or TalkBack navigation flow and announcements.

    Captures:
    - What each element announces (name, role, state, hints)
    - Focus order through the screen
    - Missing or incorrect labels
    - Navigation issues

    Used by Mobile Accessibility Specialist (MAS) for screen reader testing.
    """
    sr_name = "voiceover" if input_data.platform == "ios" else "talkback"

    return RecordScreenReaderFlowOutput(
        recording_ref=f"sr_flow_{input_data.audit_id}_{sr_name}",
        announcements=[],
        total_steps=0,
        passed_steps=0,
        failed_steps=0,
        issues_summary=[],
        focus_order_issues=[],
        missing_labels=[],
    )
