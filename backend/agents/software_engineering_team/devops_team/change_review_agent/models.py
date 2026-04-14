"""Models for change review agent."""

from __future__ import annotations

from typing import Dict, List

from devops_team.models import ReviewFinding
from pydantic import BaseModel, Field


class ChangeReviewInput(BaseModel):
    task_description: str = ""
    artifacts: Dict[str, str] = Field(default_factory=dict)


class ChangeReviewOutput(BaseModel):
    # Default True matches the legacy ``data.get("approved", not blocking)``
    # semantics: absent the LLM explicitly flagging approved=False, the
    # agent's post-processing re-derives approval from blocking findings.
    approved: bool = True
    findings: List[ReviewFinding] = Field(default_factory=list)
    summary: str = ""
