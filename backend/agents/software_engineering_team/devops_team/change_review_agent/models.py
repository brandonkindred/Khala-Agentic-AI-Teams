"""Models for change review agent."""

from __future__ import annotations

from typing import Dict, List

from devops_team.models import ReviewFinding
from pydantic import BaseModel, Field


class ChangeReviewInput(BaseModel):
    task_description: str = ""
    artifacts: Dict[str, str] = Field(default_factory=dict)


class ChangeReviewOutput(BaseModel):
    approved: bool = False
    findings: List[ReviewFinding] = Field(default_factory=list)
    summary: str = ""
