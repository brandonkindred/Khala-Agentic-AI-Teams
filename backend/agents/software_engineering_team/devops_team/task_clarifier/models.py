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
    # Default True matches the legacy ``data.get("approved_for_execution", True)``
    # semantics: absent the LLM explicitly flagging the task as incomplete,
    # we assume approved. The agent's static gap-detection runs first and
    # returns ``approved_for_execution=False`` explicitly when required
    # fields are missing, so this default only bites after the LLM path.
    approved_for_execution: bool = True
    checklist: List[str] = Field(default_factory=list)
    gaps: List[ClarificationGap] = Field(default_factory=list)
    clarification_requests: List[str] = Field(default_factory=list)
