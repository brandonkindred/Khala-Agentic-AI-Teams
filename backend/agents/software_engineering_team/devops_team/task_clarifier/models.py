"""Models for the DevOps task clarifier agent."""

from __future__ import annotations

from typing import List

from devops_team.models import DevOpsTaskSpec
from pydantic import BaseModel, Field


class ClarificationGap(BaseModel):
    area: str
    message: str
    blocking: bool = True


class DevOpsTaskClarifierInput(BaseModel):
    task_spec: DevOpsTaskSpec


class DevOpsTaskClarifierOutput(BaseModel):
    approved_for_execution: bool = False
    checklist: List[str] = Field(default_factory=list)
    gaps: List[ClarificationGap] = Field(default_factory=list)
    clarification_requests: List[str] = Field(default_factory=list)
