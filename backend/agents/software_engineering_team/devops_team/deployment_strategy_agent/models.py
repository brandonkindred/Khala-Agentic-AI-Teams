"""Models for deployment strategy agent."""

from __future__ import annotations

from typing import Dict, List

from devops_team.models import DevOpsTaskSpec
from pydantic import BaseModel, Field


class DeploymentStrategyAgentInput(BaseModel):
    task_spec: DevOpsTaskSpec


class DeploymentStrategyAgentOutput(BaseModel):
    artifacts: Dict[str, str] = Field(default_factory=dict)
    strategy: str = ""
    rollback_plan: List[str] = Field(default_factory=list)
    health_checks: List[str] = Field(default_factory=list)
    rollout_timeout_minutes: int = 15
    summary: str = ""
