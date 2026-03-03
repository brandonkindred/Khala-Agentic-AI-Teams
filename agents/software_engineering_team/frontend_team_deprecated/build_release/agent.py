"""Build and Release (Frontend DevOps) agent: CI, preview envs, release, source maps."""

from __future__ import annotations

import logging
from typing import Optional

from software_engineering_team.shared.llm import LLMClient
from software_engineering_team.shared.models import SystemArchitecture

from .models import BuildReleaseInput, BuildReleaseOutput
from .prompts import BUILD_RELEASE_PROMPT

logger = logging.getLogger(__name__)


class BuildReleaseAgent:
    """Agent that owns CI, builds, deployments, preview environments, release safety."""

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def run(self, input_data: BuildReleaseInput) -> BuildReleaseOutput:
        """Produce CI, preview, release, and observability plan for frontend repo."""
        logger.info("Build/Release: starting for task %s", input_data.task_id or "unknown")
        context_parts = [
            f"**Task:** {input_data.task_description}",
        ]
        if input_data.repo_code_summary:
            context_parts.append(f"**Repo summary:** {input_data.repo_code_summary}")
        if input_data.spec_content:
            context_parts.append(f"**Spec (excerpt):**\n{input_data.spec_content[:4000]}")
        if input_data.architecture:
            context_parts.append(f"**Architecture:** {input_data.architecture.overview}")
        if input_data.existing_pipeline:
            context_parts.append(f"**Existing pipeline:**\n{input_data.existing_pipeline[:3000]}")

        prompt = BUILD_RELEASE_PROMPT + "\n\n---\n\n" + "\n\n".join(context_parts)
        data = self.llm.complete_json(prompt, temperature=0.2)

        return BuildReleaseOutput(
            ci_plan=data.get("ci_plan", "") or "",
            preview_env_plan=data.get("preview_env_plan", "") or "",
            release_rollback_plan=data.get("release_rollback_plan", "") or "",
            source_maps_error_reporting=data.get("source_maps_error_reporting", "") or "",
            pipeline_yaml=data.get("pipeline_yaml", "") or "",
            summary=data.get("summary", "") or "",
        )
