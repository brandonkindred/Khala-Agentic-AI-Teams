"""Accessibility Expert agent: WCAG 2.2 compliance review."""

from __future__ import annotations

import json
import logging

from llm_service import get_strands_model
from strands import Agent

from .models import AccessibilityInput, AccessibilityIssue, AccessibilityOutput
from .prompts import ACCESSIBILITY_PROMPT

logger = logging.getLogger(__name__)


class AccessibilityExpertAgent:
    """
    Accessibility expert that reviews frontend code for WCAG 2.2 compliance
    and produces a list of issues for the coding agent to fix.
    """

    def __init__(self, llm_client=None) -> None:
        self._agent = Agent(model=get_strands_model("accessibility"), system_prompt=ACCESSIBILITY_PROMPT)

    def run(self, input_data: AccessibilityInput) -> AccessibilityOutput:
        """Review code for WCAG 2.2 compliance and produce issue list."""
        logger.info("Accessibility: reviewing %s chars of code", len(input_data.code or ""))
        context_parts = [
            f"**Language:** {input_data.language}",
            "**Code to review:**",
            "```",
            input_data.code,
            "```",
        ]
        if input_data.task_description:
            context_parts.insert(2, f"**Task:** {input_data.task_description}")
        if input_data.architecture:
            context_parts.append(f"**Architecture:** {input_data.architecture.overview}")

        prompt = "\n".join(context_parts)
        result = self._agent(prompt)
        raw = (result.message if hasattr(result, "message") else str(result)).strip()
        data = json.loads(raw)

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
