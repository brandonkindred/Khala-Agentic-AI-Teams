"""Models for DevSecOps review agent."""

from __future__ import annotations

from typing import Dict, List

from pydantic import BaseModel, Field

from devops_team.models import ReviewFinding


class DevSecOpsReviewInput(BaseModel):
    task_description: str = ""
    requirements: str = ""
    artifacts: Dict[str, str] = Field(default_factory=dict)


class DevSecOpsReviewOutput(BaseModel):
    approved: bool = False
    findings: List[ReviewFinding] = Field(default_factory=list)
    summary: str = ""
