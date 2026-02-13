"""DevOps Expert agent: CI/CD, IaC, Docker, networking."""

from __future__ import annotations

import logging
from typing import Any, Dict

from shared.llm import LLMClient

from .models import DevOpsInput, DevOpsOutput
from .prompts import DEVOPS_PROMPT

logger = logging.getLogger(__name__)


class DevOpsExpertAgent:
    """
    DevOps expert specializing in CI/CD pipelines, IaC, Dockerization, and networking.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def run(self, input_data: DevOpsInput) -> DevOpsOutput:
        """Create or extend CI/CD, IaC, and Docker configurations."""
        logger.info("DevOps: starting task '%s'", input_data.task_description[:60] + ("..." if len(input_data.task_description) > 60 else ""))
        context_parts = [
            f"**Task:** {input_data.task_description}",
            f"**Requirements:** {input_data.requirements}",
        ]
        if input_data.architecture:
            context_parts.extend([
                "",
                "**Architecture:**",
                input_data.architecture.overview,
                *[f"- {c.name} ({c.type}): {c.technology or 'TBD'}" for c in input_data.architecture.components],
            ])
        if input_data.existing_pipeline:
            context_parts.extend(["", "**Existing Pipeline:**", input_data.existing_pipeline])
        if input_data.tech_stack:
            context_parts.extend(["", "**Tech Stack:**", ", ".join(input_data.tech_stack)])
        if getattr(input_data, "target_repo", None):
            context_parts.extend([
                "",
                "**Target repo:** You are producing containerization and deployment artifacts for this application repo only.",
                f"- target_repo={input_data.target_repo}",
            ])

        prompt = DEVOPS_PROMPT + "\n\n---\n\n" + "\n".join(context_parts)
        data = self.llm.complete_json(prompt, temperature=0.2)

        summary = data.get("summary", "")
        needs_clarification = bool(data.get("needs_clarification", False))
        clarification_requests = data.get("clarification_requests") or []
        if not isinstance(clarification_requests, list):
            clarification_requests = [str(clarification_requests)] if clarification_requests else []

        logger.info(
            "DevOps: done, summary=%s chars, needs_clarification=%s",
            len(summary), needs_clarification,
        )
        return DevOpsOutput(
            pipeline_yaml=data.get("pipeline_yaml", ""),
            iac_content=data.get("iac_content", ""),
            dockerfile=data.get("dockerfile", ""),
            docker_compose=data.get("docker_compose", ""),
            summary=summary,
            artifacts=data.get("artifacts", {}),
            suggested_commit_message=data.get("suggested_commit_message", ""),
            needs_clarification=needs_clarification,
            clarification_requests=clarification_requests,
        )
