"""Models for CI/CD pipeline agent."""

from __future__ import annotations

from typing import Dict, List

from pydantic import BaseModel, Field

from devops_team.models import DevOpsTaskSpec


class CICDPipelineAgentInput(BaseModel):
    task_spec: DevOpsTaskSpec
    existing_pipeline: str = ""


class CICDPipelineAgentOutput(BaseModel):
    artifacts: Dict[str, str] = Field(default_factory=dict)
    pipeline_job_graph_summary: str = ""
    required_gates_present: bool = False
    summary: str = ""
    risks: List[str] = Field(default_factory=list)
