"""Models for DevOps test validation agent."""

from __future__ import annotations

from typing import Dict, List

from devops_team.models import GateStatus
from pydantic import BaseModel, Field


class ValidationEvidence(BaseModel):
    gate: str
    status: GateStatus
    detail: str = ""


class DevOpsTestValidationInput(BaseModel):
    acceptance_criteria: List[str] = Field(default_factory=list)
    tool_results: Dict[str, Dict[str, str]] = Field(default_factory=dict)


class DevOpsTestValidationOutput(BaseModel):
    approved: bool = False
    quality_gates: Dict[str, GateStatus] = Field(default_factory=dict)
    acceptance_trace: List[Dict[str, object]] = Field(default_factory=list)
    evidence: List[ValidationEvidence] = Field(default_factory=list)
    summary: str = ""
