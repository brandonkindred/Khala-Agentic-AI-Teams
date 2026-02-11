"""Frontend Expert agent: Angular implementation."""

from __future__ import annotations

import logging
from typing import Any, Dict

from shared.llm import LLMClient

from .models import FrontendInput, FrontendOutput
from .prompts import FRONTEND_PROMPT

logger = logging.getLogger(__name__)


class FrontendExpertAgent:
    """
    Frontend expert that implements solutions using Angular.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def run(self, input_data: FrontendInput) -> FrontendOutput:
        """Implement frontend functionality in Angular."""
        logger.info("Frontend: implementing task '%s'", input_data.task_description[:60] + ("..." if len(input_data.task_description) > 60 else ""))
        context_parts = [
            f"**Task:** {input_data.task_description}",
            f"**Requirements:** {input_data.requirements}",
        ]
        if input_data.architecture:
            context_parts.extend([
                "",
                "**Architecture:**",
                input_data.architecture.overview,
                *[f"- {c.name} ({c.type})" for c in input_data.architecture.components if c.type == "frontend"],
            ])
        if input_data.existing_code:
            context_parts.extend(["", "**Existing code:**", input_data.existing_code])
        if input_data.api_endpoints:
            context_parts.extend(["", "**API endpoints:**", input_data.api_endpoints])
        if input_data.qa_issues:
            qa_text = "\n".join(
                f"- [{i.get('severity')}] {i.get('description')} (location: {i.get('location')})\n  Recommendation: {i.get('recommendation')}"
                for i in input_data.qa_issues
            )
            context_parts.extend(["", "**QA issues to fix (implement these):**", qa_text])
        if input_data.security_issues:
            sec_text = "\n".join(
                f"- [{i.get('severity')}] {i.get('category')}: {i.get('description')} (location: {i.get('location')})\n  Recommendation: {i.get('recommendation')}"
                for i in input_data.security_issues
            )
            context_parts.extend(["", "**Security issues to fix (implement these):**", sec_text])

        prompt = FRONTEND_PROMPT + "\n\n---\n\n" + "\n".join(context_parts)
        data = self.llm.complete_json(prompt, temperature=0.2)

        code = data.get("code", "")
        if code and "\\n" in code:
            code = code.replace("\\n", "\n")

        summary = data.get("summary", "")
        logger.info("Frontend: done, code=%s chars, summary=%s chars", len(code), len(summary))
        return FrontendOutput(
            code=code,
            summary=summary,
            files=data.get("files", {}),
            components=data.get("components", []),
            suggested_commit_message=data.get("suggested_commit_message", ""),
        )
