"""Performance Engineer agent: budgets, code splitting, caching, profiling."""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from llm_service import LLMClient

from .models import PerformanceEngineerInput, PerformanceEngineerOutput
from .prompts import PERFORMANCE_ENGINEER_PROMPT

logger = logging.getLogger(__name__)


class PerformanceEngineerAgent:
    """Agent that owns speed, responsiveness, bundle size, runtime cost."""

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def run(self, input_data: PerformanceEngineerInput) -> PerformanceEngineerOutput:
        """Review code for performance; return issues and recommendations."""
        logger.info(
            "Performance Engineer: reviewing %s chars for task %s",
            len(input_data.code or ""),
            input_data.task_id or "unknown",
        )
        context_parts = [
            f"**Task:** {input_data.task_description}",
            "**Code to review:**",
            "```",
            (input_data.code or "")[:25000],
            "```",
        ]
        if input_data.build_output:
            context_parts.append(f"**Build output:**\n{input_data.build_output[:2000]}")
        if input_data.architecture:
            context_parts.insert(2, f"**Architecture:** {input_data.architecture.overview}")

        prompt = PERFORMANCE_ENGINEER_PROMPT + "\n\n---\n\n" + "\n".join(context_parts)
        data = self.llm.complete_json(prompt, temperature=0.1)

        issues: List[Dict[str, Any]] = []
        for i in data.get("issues") or []:
            if isinstance(i, dict) and i.get("description"):
                issues.append(
                    {
                        "severity": i.get("severity", "medium"),
                        "category": i.get("category", "performance"),
                        "file_path": i.get("file_path", ""),
                        "description": i["description"],
                        "suggestion": i.get("suggestion", ""),
                    }
                )

        critical = [x for x in issues if x.get("severity") == "critical"]
        approved = data.get("approved", len(critical) == 0)

        logger.info("Performance Engineer: %s issues, approved=%s", len(issues), approved)
        return PerformanceEngineerOutput(
            issues=issues,
            approved=approved,
            performance_budgets=data.get("performance_budgets", "") or "",
            code_splitting_plan=data.get("code_splitting_plan", "") or "",
            caching_strategy=data.get("caching_strategy", "") or "",
            summary=data.get("summary", "") or "",
        )
