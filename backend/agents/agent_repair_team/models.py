"""Models for the Repair Expert agent."""

from pathlib import Path
from typing import Any, Dict, List

from pydantic import BaseModel, Field


class RepairInput(BaseModel):
    """Input for the Repair Expert agent."""

    traceback: str = Field(description="Full traceback string from the crashed agent")
    exception_type: str = Field(description="Exception class name (e.g. NameError, ImportError)")
    exception_message: str = Field(description="Exception message")
    task_id: str = Field(description="Task ID that was running when the crash occurred")
    agent_type: str = Field(description="Agent type: backend or frontend")
    agent_source_path: Path = Field(
        description="Path to software_engineering_team/ or repo root; edits must be under this tree"
    )


class RepairOutput(BaseModel):
    """Output from the Repair Expert agent."""

    suggested_fixes: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="List of fixes: each has file_path, line_start, line_end (or line), replacement_content",
    )
    summary: str = Field(default="", description="Brief summary of what was fixed")
    applied: bool = Field(
        default=False,
        description="True if the caller applied the fixes; set by orchestrator after validation",
    )
