"""Models for the DevOps Expert agent."""

from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from shared.models import SystemArchitecture


class DevOpsInput(BaseModel):
    """Input for the DevOps Expert agent."""

    task_description: str
    requirements: str = ""
    architecture: Optional[SystemArchitecture] = None
    existing_pipeline: Optional[str] = None
    tech_stack: Optional[List[str]] = None  # e.g. ["python", "docker", "kubernetes"]


class DevOpsOutput(BaseModel):
    """Output from the DevOps Expert agent."""

    pipeline_yaml: str = Field(default="", description="CI/CD pipeline configuration")
    iac_content: str = Field(default="", description="Infrastructure as Code (Terraform, CloudFormation, etc.)")
    dockerfile: str = Field(default="", description="Dockerfile content")
    docker_compose: str = Field(default="", description="Docker Compose if applicable")
    summary: str = ""
    artifacts: Dict[str, str] = Field(default_factory=dict)
