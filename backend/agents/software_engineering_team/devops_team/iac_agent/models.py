"""Models for IaC agent."""

from __future__ import annotations

from typing import Dict, List

from devops_team.models import DevOpsTaskSpec
from pydantic import BaseModel, Field


class IaCAgentInput(BaseModel):
    task_spec: DevOpsTaskSpec
    repo_summary: str = ""


class IaCAgentOutput(BaseModel):
    artifacts: Dict[str, str] = Field(default_factory=dict)
    summary: str = ""
    plan_summary: str = ""
    destructive_changes_detected: bool = False
    blast_radius_notes: List[str] = Field(default_factory=list)
