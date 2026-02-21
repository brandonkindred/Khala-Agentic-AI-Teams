"""Integration / API-contract agent: validates full-stack backend-frontend alignment."""

from __future__ import annotations

import logging
from typing import Any

from shared.llm import LLMClient

from .models import IntegrationInput, IntegrationIssue, IntegrationOutput
from .prompts import INTEGRATION_PROMPT

logger = logging.getLogger(__name__)


class IntegrationAgent:
    """
    Integration expert that validates backend API and frontend are correctly aligned.
    Detects contract mismatches, missing endpoints, wrong payload shapes.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def run(self, input_data: IntegrationInput) -> IntegrationOutput:
        """Analyze backend and frontend code for integration/contract issues."""
        logger.info(
            "Integration: analyzing backend (%s chars) and frontend (%s chars)",
            len(input_data.backend_code or ""),
            len(input_data.frontend_code or ""),
        )

        context_parts = [
            "**Backend code:**",
            "```",
            input_data.backend_code or "# No backend code",
            "```",
            "",
            "**Frontend code:**",
            "```",
            input_data.frontend_code or "# No frontend code",
            "```",
        ]
        if input_data.spec_content:
            context_parts.extend([
                "",
                "**Project specification:**",
                "---",
                input_data.spec_content[:8000],
                "---",
            ])
        if input_data.architecture:
            context_parts.append(f"**Architecture:** {input_data.architecture.overview}")

        prompt = INTEGRATION_PROMPT + "\n\n---\n\n" + "\n".join(context_parts)
        data = self.llm.complete_json(prompt, temperature=0.1)

        issues = []
        for i in data.get("issues") or []:
            if isinstance(i, dict) and i.get("description"):
                issues.append(
                    IntegrationIssue(
                        severity=i.get("severity", "medium"),
                        category=i.get("category", "contract_mismatch"),
                        description=i["description"],
                        backend_location=i.get("backend_location", ""),
                        frontend_location=i.get("frontend_location", ""),
                        recommendation=i.get("recommendation", ""),
                    )
                )

        critical_issues = [i for i in issues if i.severity in ("critical", "high")]
        passed = len(critical_issues) == 0

        fix_suggestions = data.get("fix_task_suggestions") or []
        if not isinstance(fix_suggestions, list):
            fix_suggestions = []

        logger.info(
            "Integration: done, %s issues (%s critical/high), passed=%s",
            len(issues),
            len(critical_issues),
            passed,
        )
        return IntegrationOutput(
            passed=passed,
            issues=issues,
            summary=data.get("summary", ""),
            fix_task_suggestions=fix_suggestions,
        )
