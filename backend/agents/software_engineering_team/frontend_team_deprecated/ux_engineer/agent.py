"""UX Engineer agent: interaction polish, keyboard, usability, delight with restraint."""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from llm_service import LLMClient

from .models import UXEngineerInput, UXEngineerOutput
from .prompts import UX_ENGINEER_PROMPT

logger = logging.getLogger(__name__)


class UXEngineerAgent:
    """Agent that owns the feel of the product: performance perception, interaction polish, usability."""

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def run(self, input_data: UXEngineerInput) -> UXEngineerOutput:
        """Review code for UX polish; return issues as code_review-style for implementation pass."""
        logger.info(
            "UX Engineer: reviewing %s chars for task %s",
            len(input_data.code or ""),
            input_data.task_id or "unknown",
        )
        context_parts = [
            f"**Task:** {input_data.task_description}",
            "**Code to review:**",
            "```",
            (input_data.code or "")[:30000],
            "```",
        ]
        if input_data.architecture:
            context_parts.insert(2, f"**Architecture:** {input_data.architecture.overview}")

        prompt = UX_ENGINEER_PROMPT + "\n\n---\n\n" + "\n".join(context_parts)
        data = self.llm.complete_json(prompt, temperature=0.1, think=True)

        issues: List[Dict[str, Any]] = []
        for i in data.get("issues") or []:
            if isinstance(i, dict) and i.get("description"):
                issues.append(
                    {
                        "severity": i.get("severity", "medium"),
                        "category": i.get("category", "ux"),
                        "file_path": i.get("file_path", ""),
                        "description": i["description"],
                        "suggestion": i.get("suggestion", ""),
                    }
                )

        critical_major = [x for x in issues if x.get("severity") in ("critical", "major")]
        approved = data.get("approved", len(critical_major) == 0)

        logger.info("UX Engineer: %s issues, approved=%s", len(issues), approved)
        return UXEngineerOutput(
            issues=issues,
            approved=approved,
            summary=data.get("summary", "") or "",
        )
