"""Models for the DevOps Review agent."""

from typing import List, Optional

from pydantic import BaseModel, Field


class DevOpsReviewIssue(BaseModel):
    """A single issue found during DevOps artifact review."""

    severity: str = Field(
        default="major",
        description="Severity: critical, major, minor, or nit",
    )
    artifact: str = Field(
        default="",
        description="Which artifact has the issue: Dockerfile, pipeline_yaml, docker_compose, iac_content",
    )
    description: str = Field(
        default="",
        description="Clear description of the issue",
    )
    suggestion: str = Field(
        default="",
        description="Concrete suggestion for how to fix the issue",
    )


class DevOpsReviewInput(BaseModel):
    """Input for the DevOps Review agent."""

    dockerfile: str = Field(default="", description="Dockerfile content to review")
    pipeline_yaml: str = Field(default="", description="CI/CD pipeline YAML to review")
    docker_compose: str = Field(default="", description="docker-compose.yml content to review")
    iac_content: str = Field(default="", description="IaC content (Terraform, etc.) to review")
    task_description: str = Field(default="", description="Task the DevOps agent was working on")
    requirements: str = Field(default="", description="Requirements for the task")
    target_repo: Optional[str] = Field(
        default=None,
        description="Target repo: backend (Python/FastAPI) or frontend (Angular/Node)",
    )


class DevOpsReviewOutput(BaseModel):
    """Output from the DevOps Review agent."""

    approved: bool = Field(
        default=False,
        description="True when artifacts pass review (no critical or major issues)",
    )
    issues: List[DevOpsReviewIssue] = Field(
        default_factory=list,
        description="List of issues found during review",
    )
    summary: str = Field(
        default="",
        description="Overall summary of the review",
    )
