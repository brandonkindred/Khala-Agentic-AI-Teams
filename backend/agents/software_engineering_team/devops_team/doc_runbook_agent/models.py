"""Models for documentation and runbook agent."""

from __future__ import annotations

from typing import Dict, List

from pydantic import BaseModel, Field

from devops_team.models import DevOpsCompletionPackage


class DocumentationRunbookInput(BaseModel):
    task_id: str
    task_title: str = ""
    artifacts: Dict[str, str] = Field(default_factory=dict)
    quality_gates: Dict[str, str] = Field(default_factory=dict)
    notes: List[str] = Field(default_factory=list)


class DocumentationRunbookOutput(BaseModel):
    files: Dict[str, str] = Field(default_factory=dict)
    completion_package: DevOpsCompletionPackage
    summary: str = ""
