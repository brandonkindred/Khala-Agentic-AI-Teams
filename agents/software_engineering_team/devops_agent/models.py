"""Models for the DevOps Expert agent."""

from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from software_engineering_team.shared.models import SystemArchitecture


class TargetRepo(str, Enum):
    """Which application repo to containerize."""

    BACKEND = "backend"
    FRONTEND = "frontend"


class DevOpsInput(BaseModel):
    """Input for the DevOps Expert agent."""

    task_description: str
    requirements: str = ""
    architecture: Optional[SystemArchitecture] = None
    existing_pipeline: Optional[str] = None
    tech_stack: Optional[List[str]] = None  # e.g. ["python", "docker", "kubernetes"]
    target_repo: Optional[TargetRepo] = Field(
        default=None,
        description="Which application repo to containerize: backend (Python/FastAPI) or frontend (Node/React/Angular/Vue). When set, produce a Dockerfile and CI appropriate for that repo only.",
    )
    task_plan: Optional[str] = Field(
        default=None,
        description="Implementation plan from _plan_task(). When present, implement according to this plan.",
    )
    build_errors: Optional[str] = Field(
        default=None,
        description="Build/validation errors from previous run. When present, fix these issues in the config.",
    )


class DevOpsOutput(BaseModel):
    """Output from the DevOps Expert agent."""

    pipeline_yaml: str = Field(default="", description="CI/CD pipeline configuration")
    iac_content: str = Field(default="", description="Infrastructure as Code (Terraform, CloudFormation, etc.)")
    dockerfile: str = Field(default="", description="Dockerfile content")
    docker_compose: str = Field(default="", description="Docker Compose if applicable")
    summary: str = ""
    artifacts: Dict[str, str] = Field(default_factory=dict)
    suggested_commit_message: str = Field(
        default="",
        description="Conventional Commits format, e.g. ci: add GitHub Actions pipeline",
    )
    needs_clarification: bool = Field(
        default=False,
        description="When True, task is ambiguous; do not implement until clarification_requests are answered",
    )
    clarification_requests: List[str] = Field(
        default_factory=list,
        description="Specific questions for Tech Lead when task is poorly defined",
    )


class DevOpsWorkflowResult(BaseModel):
    """Result of the DevOps workflow (plan -> generate -> write -> verify loop)."""

    success: bool = Field(description="True when verification passed")
    failure_reason: Optional[str] = Field(
        default=None,
        description="Reason for failure when success is False",
    )
    iterations: int = Field(
        default=1,
        description="Number of generate/verify iterations performed",
    )
