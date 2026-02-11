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

        prompt = FRONTEND_PROMPT + "\n\n---\n\n" + "\n".join(context_parts)
        data = self.llm.complete_json(prompt, temperature=0.2)

        code = data.get("code", "")
        if code and "\\n" in code:
            code = code.replace("\\n", "\n")

        return FrontendOutput(
            code=code,
            summary=data.get("summary", ""),
            files=data.get("files", {}),
            components=data.get("components", []),
        )
