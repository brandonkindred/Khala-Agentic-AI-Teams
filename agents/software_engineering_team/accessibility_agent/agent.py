"""Accessibility Expert agent: WCAG 2.2 compliance review."""

from __future__ import annotations

import logging

from software_engineering_team.shared.llm import LLMClient

from .models import AccessibilityInput, AccessibilityIssue, AccessibilityOutput
from .prompts import ACCESSIBILITY_PROMPT

logger = logging.getLogger(__name__)


class AccessibilityExpertAgent:
    """
    Accessibility expert that reviews frontend code for WCAG 2.2 compliance
    and produces a list of issues for the coding agent to fix.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def run(self, input_data: AccessibilityInput) -> AccessibilityOutput:
        """Review code for WCAG 2.2 compliance and produce issue list."""
        logger.info("Accessibility: reviewing %s chars of code", len(input_data.code or ""))
        context_parts = [
            f"**Language:** {input_data.language}",
            f"**Code to review:**",
            "```",
            input_data.code,
            "```",
        ]
        if input_data.task_description:
            context_parts.insert(2, f"**Task:** {input_data.task_description}")
        if input_data.architecture:
            context_parts.append(f"**Architecture:** {input_data.architecture.overview}")

        prompt = ACCESSIBILITY_PROMPT + "\n\n---\n\n" + "\n".join(context_parts)
        data = self.llm.complete_json(prompt, temperature=0.1)

        issues = []
        for i in data.get("issues") or []:
            if isinstance(i, dict) and i.get("description"):
                issues.append(
                    AccessibilityIssue(
                        severity=i.get("severity", "medium"),
                        wcag_criterion=i.get("wcag_criterion", ""),
                        description=i["description"],
                        location=i.get("location", ""),
                        recommendation=i.get("recommendation", ""),
                    )
                )

        critical_issues = [i for i in issues if i.severity in ("critical", "high")]
        approved = len(critical_issues) == 0

        logger.info("Accessibility: done, %s issues found, approved=%s", len(issues), approved)
        return AccessibilityOutput(
            issues=issues,
            approved=approved,
            summary=data.get("summary", ""),
        )
