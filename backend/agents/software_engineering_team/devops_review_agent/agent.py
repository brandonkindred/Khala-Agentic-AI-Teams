"""DevOps Review agent: reviews Dockerfiles, CI/CD, and IaC for best practices."""

from __future__ import annotations

import logging
from typing import List

from software_engineering_team.shared.llm import LLMClient

from .models import DevOpsReviewInput, DevOpsReviewIssue, DevOpsReviewOutput
from .prompts import DEVOPS_REVIEW_PROMPT

logger = logging.getLogger(__name__)


class DevOpsReviewAgent:
    """
    DevOps review agent that reviews Dockerfiles, CI/CD pipelines, docker-compose,
    and IaC configurations for best practices and production readiness.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def run(self, input_data: DevOpsReviewInput) -> DevOpsReviewOutput:
        """Review DevOps artifacts and return approval or issues."""
        context_parts = [
            f"**Task:** {input_data.task_description}",
            f"**Requirements:** {input_data.requirements}",
        ]
        if input_data.target_repo:
            context_parts.append(f"**Target repo:** {input_data.target_repo}")

        if input_data.dockerfile:
            context_parts.extend([
                "",
                "**Dockerfile:**",
                "```",
                input_data.dockerfile,
                "```",
            ])
        if input_data.pipeline_yaml:
            context_parts.extend([
                "",
                "**CI/CD Pipeline:**",
                "```yaml",
                input_data.pipeline_yaml,
                "```",
            ])
        if input_data.docker_compose:
            context_parts.extend([
                "",
                "**docker-compose.yml:**",
                "```yaml",
                input_data.docker_compose,
                "```",
            ])
        if input_data.iac_content:
            context_parts.extend([
                "",
                "**IaC:**",
                "```",
                input_data.iac_content,
                "```",
            ])

        if not any([input_data.dockerfile, input_data.pipeline_yaml, input_data.docker_compose, input_data.iac_content]):
            logger.warning("DevOpsReview: no artifacts to review")
            return DevOpsReviewOutput(approved=True, issues=[], summary="No artifacts to review")

        logger.info(
            "DevOpsReview: reviewing artifacts | task=%s",
            input_data.task_description[:60] if input_data.task_description else "",
        )

        prompt = DEVOPS_REVIEW_PROMPT + "\n\n---\n\n" + "\n".join(context_parts)
        data = self.llm.complete_json(prompt, temperature=0.1)

        issues: List[DevOpsReviewIssue] = []
        for issue_data in data.get("issues") or []:
            if isinstance(issue_data, dict) and issue_data.get("description"):
                issues.append(
                    DevOpsReviewIssue(
                        severity=issue_data.get("severity", "major"),
                        artifact=issue_data.get("artifact", ""),
                        description=issue_data.get("description", ""),
                        suggestion=issue_data.get("suggestion", ""),
                    )
                )

        critical_or_major = [i for i in issues if i.severity in ("critical", "major")]
        raw_approved = bool(data.get("approved", False))
        approved = raw_approved and len(critical_or_major) == 0

        # Safety net: rejected with no actionable issues
        if not approved and not critical_or_major:
            summary_text = data.get("summary", "")
            if issues:
                # Only minor/nit issues
                logger.info(
                    "DevOpsReview: overriding to approved=True (only %s minor/nit issues)",
                    len(issues),
                )
                approved = True
            elif summary_text and summary_text.strip():
                synthesized = DevOpsReviewIssue(
                    severity="major",
                    artifact="",
                    description=f"DevOps review rejected: {summary_text}",
                    suggestion="Address the concerns in the review summary. Ensure Dockerfile, CI/CD, and IaC follow best practices.",
                )
                issues.append(synthesized)
                critical_or_major.append(synthesized)
            else:
                logger.warning("DevOpsReview: approved=False with no issues/summary, auto-approving")
                approved = True

        logger.info(
            "DevOpsReview: done, approved=%s, issues=%s (critical/major=%s)",
            approved,
            len(issues),
            len(critical_or_major),
        )

        return DevOpsReviewOutput(
            approved=approved,
            issues=issues,
            summary=data.get("summary", ""),
        )
