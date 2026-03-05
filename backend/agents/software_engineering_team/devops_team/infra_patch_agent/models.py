"""Models for the Infrastructure Patch agent."""

from __future__ import annotations

from typing import Dict

from pydantic import BaseModel, Field

from devops_team.infra_debug_agent.models import IaCDebugOutput


class IaCPatchInput(BaseModel):
    """Input for the Infrastructure Patch agent."""

    debug_output: IaCDebugOutput = Field(description="Classified errors from debug agent")
    original_artifacts: Dict[str, str] = Field(
        description="Current IaC artifact file contents keyed by path",
    )
    repo_path: str = ""


class IaCPatchOutput(BaseModel):
    """Output from the Infrastructure Patch agent."""

    patched_artifacts: Dict[str, str] = Field(
        default_factory=dict,
        description="Updated artifact contents keyed by file path",
    )
    summary: str = ""
    edits_applied: int = 0
