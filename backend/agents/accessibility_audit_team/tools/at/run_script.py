"""
Tool: at.run_script

Run a standardized AT script and capture results.
"""

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class ATScript(BaseModel):
    """Assistive technology test script."""

    name: str = Field(..., description="Script name")
    steps: List[str] = Field(default_factory=list, description="Steps to execute")


class ATStepResult(BaseModel):
    """Result of a single AT script step."""

    step: int = Field(..., description="Step number")
    instruction: str = Field(default="", description="What was attempted")
    expected: str = Field(default="", description="Expected behavior/announcement")
    actual: str = Field(default="", description="Actual behavior/announcement")
    passed: bool = Field(default=True)
    evidence_ref: str = Field(default="", description="Reference to evidence")
    notes: str = Field(default="")


class TargetInfo(BaseModel):
    """Target being tested."""

    url: Optional[str] = Field(default=None, description="Web URL")
    screen: Optional[str] = Field(default=None, description="Mobile screen")


class RunScriptInput(BaseModel):
    """Input for running an AT script."""

    audit_id: str = Field(..., description="Audit identifier")
    surface: Literal["web", "ios", "android"] = Field(..., description="Platform surface")
    tool: Literal["nvda", "jaws", "voiceover", "talkback"] = Field(
        ..., description="Assistive technology to use"
    )
    script: ATScript
    target: TargetInfo
    capture: Dict[str, bool] = Field(
        default_factory=lambda: {"audio": False, "notes": True, "video": False}
    )


class RunScriptOutput(BaseModel):
    """Output from running an AT script."""

    script_name: str
    tool: str
    step_results: List[ATStepResult] = Field(default_factory=list)
    total_steps: int = Field(default=0)
    passed_steps: int = Field(default=0)
    failed_steps: int = Field(default=0)
    summary: str = Field(default="")
    recording_ref: str = Field(default="", description="Audio/video recording ref")


async def run_script(input_data: RunScriptInput) -> RunScriptOutput:
    """
    Run a standardized assistive technology script and capture results.

    This tool executes pre-defined AT verification scripts that test:
    - What is announced (name, role, value, state)
    - Navigation patterns (headings, landmarks, rotor)
    - Form mode behavior and error messaging
    - Reading order and content structure

    Used by Assistive Technology Specialist (ATS) to verify findings
    and provide AT-verified impact statements.

    This is the "truth layer" that validates or invalidates automated scan signals.
    """
    return RunScriptOutput(
        script_name=input_data.script.name,
        tool=input_data.tool,
        step_results=[],
        total_steps=len(input_data.script.steps),
        passed_steps=0,
        failed_steps=0,
        summary="",
        recording_ref="",
    )
