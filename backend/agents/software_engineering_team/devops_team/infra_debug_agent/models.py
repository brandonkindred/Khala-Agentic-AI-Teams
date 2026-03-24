"""Models for the Infrastructure Debug agent."""

from __future__ import annotations

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class IaCExecutionError(BaseModel):
    """A classified IaC execution error."""

    error_type: Literal[
        "syntax",
        "state",
        "permissions",
        "resource_conflict",
        "validation",
        "runtime",
        "unknown",
    ] = "unknown"
    tool: str = ""
    file_path: Optional[str] = None
    line_number: Optional[int] = None
    error_message: str = ""
    raw_output: str = ""


class IaCDebugInput(BaseModel):
    """Input for the Infrastructure Debug agent."""

    execution_output: str = Field(description="Raw CLI output from the failed execution")
    tool_name: str = Field(description="Tool that failed: terraform, cdk, compose, helm")
    command: str = Field(description="Command that was run: plan, synth, config, etc.")
    artifacts: Dict[str, str] = Field(
        default_factory=dict,
        description="Current IaC artifact file contents",
    )


class IaCDebugOutput(BaseModel):
    """Output from the Infrastructure Debug agent."""

    errors: List[IaCExecutionError] = Field(default_factory=list)
    summary: str = ""
    fixable: bool = False
