"""Models for the Build and Release (Frontend DevOps) agent."""

from typing import Optional

from pydantic import BaseModel, Field

from software_engineering_team.shared.models import SystemArchitecture


class BuildReleaseInput(BaseModel):
    """Input for the Build and Release agent."""

    task_description: str = ""
    task_id: str = ""
    spec_content: str = ""
    architecture: Optional[SystemArchitecture] = None
    existing_pipeline: Optional[str] = None  # Existing CI config (YAML)
    repo_code_summary: str = ""  # Brief summary of frontend stack


class BuildReleaseOutput(BaseModel):
    """Output from the Build and Release agent."""

    ci_plan: str = Field(
        default="",
        description="CI checks: lint, typecheck, tests, bundle analysis, vuln scan",
    )
    preview_env_plan: str = Field(
        default="",
        description="Preview environment plan (per PR)",
    )
    release_rollback_plan: str = Field(
        default="",
        description="Release and rollback plan",
    )
    source_maps_error_reporting: str = Field(
        default="",
        description="Source maps, error reporting integration, artifact retention",
    )
    pipeline_yaml: str = Field(
        default="",
        description="Optional: CI pipeline YAML to add or update (e.g. GitHub Actions)",
    )
    summary: str = ""
