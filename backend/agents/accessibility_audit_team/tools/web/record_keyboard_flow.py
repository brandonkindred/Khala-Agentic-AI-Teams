"""
Tool: web.record_keyboard_flow

Capture tab order and focus states for a flow.
"""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class KeyboardAction(BaseModel):
    """A single keyboard action in the flow."""

    type: Literal["key", "click", "wait"] = Field(..., description="Action type")
    value: str = Field(
        default="",
        description="Key name (Tab, Shift+Tab, Enter, Space, Escape, Arrow*) or selector or ms",
    )


class FocusTraceStep(BaseModel):
    """A single step in the focus trace."""

    step: int = Field(..., description="Step number")
    selector: str = Field(default="", description="CSS selector of focused element")
    role: str = Field(default="", description="ARIA role")
    name: str = Field(default="", description="Accessible name")
    visible_focus: bool = Field(
        default=True, description="Whether focus indicator is visible"
    )
    focus_ring_color: Optional[str] = Field(default=None)
    focus_ring_width: Optional[str] = Field(default=None)
    tab_index: Optional[int] = Field(default=None)
    is_interactive: bool = Field(default=True)


class RecordKeyboardFlowInput(BaseModel):
    """Input for recording a keyboard navigation flow."""

    audit_id: str = Field(..., description="Audit identifier")
    start_url: str = Field(..., description="URL to start from")
    actions: List[KeyboardAction] = Field(
        default_factory=list, description="Sequence of keyboard actions"
    )
    capture: Dict[str, bool] = Field(
        default_factory=lambda: {"video": False, "screenshots": True},
        description="What to capture: video, screenshots",
    )
    max_steps: int = Field(
        default=100, description="Maximum steps before stopping"
    )


class RecordKeyboardFlowOutput(BaseModel):
    """Output from recording a keyboard flow."""

    video_ref: str = Field(default="", description="Reference to video recording")
    focus_trace: List[FocusTraceStep] = Field(default_factory=list)
    screenshots: List[str] = Field(
        default_factory=list, description="Screenshot references"
    )
    issues_detected: List[Dict[str, Any]] = Field(
        default_factory=list, description="Keyboard issues detected during flow"
    )
    focus_order_valid: bool = Field(
        default=True, description="Whether focus order follows DOM order"
    )
    keyboard_traps_found: List[str] = Field(
        default_factory=list, description="Elements where keyboard got trapped"
    )


async def record_keyboard_flow(
    input_data: RecordKeyboardFlowInput,
) -> RecordKeyboardFlowOutput:
    """
    Record keyboard navigation flow capturing tab order and focus states.

    This tool captures:
    - Focus trace (which elements receive focus in what order)
    - Focus indicator visibility
    - Keyboard traps
    - Focus order vs DOM order comparison

    Used by the Web Audit Specialist (WAS) to test keyboard accessibility.
    """
    # Schema definition - actual implementation will use browser automation
    return RecordKeyboardFlowOutput(
        video_ref=f"keyboard_flow_{input_data.audit_id}" if input_data.capture.get("video") else "",
        focus_trace=[],
        screenshots=[],
        issues_detected=[],
        focus_order_valid=True,
        keyboard_traps_found=[],
    )
