"""Repo Navigator tool agent."""

from __future__ import annotations

from pathlib import Path
from typing import List

from pydantic import BaseModel, Field


class RepoNavigatorInput(BaseModel):
    repo_path: str


class RepoNavigatorOutput(BaseModel):
    detected_iac_paths: List[str] = Field(default_factory=list)
    detected_pipeline_paths: List[str] = Field(default_factory=list)
    detected_deploy_paths: List[str] = Field(default_factory=list)
    summary: str = ""


class RepoNavigatorToolAgent:
    """Discovers DevOps-relevant folders and files."""

    def run(self, input_data: RepoNavigatorInput) -> RepoNavigatorOutput:
        root = Path(input_data.repo_path).resolve()
        iac_paths: List[str] = []
        pipeline_paths: List[str] = []
        deploy_paths: List[str] = []
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            rel = str(p.relative_to(root))
            low = rel.lower()
            if low.endswith(".tf") or "/terraform/" in low or "/infra/" in low:
                iac_paths.append(rel)
            if (
                ".github/workflows/" in low
                or low.endswith(".gitlab-ci.yml")
                or "jenkinsfile" in low
            ):
                pipeline_paths.append(rel)
            if "/helm/" in low or "/k8s/" in low or "docker-compose" in low:
                deploy_paths.append(rel)
        return RepoNavigatorOutput(
            detected_iac_paths=sorted(set(iac_paths))[:200],
            detected_pipeline_paths=sorted(set(pipeline_paths))[:200],
            detected_deploy_paths=sorted(set(deploy_paths))[:200],
            summary="Repository DevOps paths discovered",
        )
