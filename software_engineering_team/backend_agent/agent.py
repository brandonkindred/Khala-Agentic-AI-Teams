"""Backend Expert agent: Python/Java implementation."""

from __future__ import annotations

import logging
from typing import Any, Dict

from shared.llm import LLMClient

from .models import BackendInput, BackendOutput
from .prompts import BACKEND_PROMPT

logger = logging.getLogger(__name__)


class BackendExpertAgent:
    """
    Backend expert that implements solutions in Python or Java
    based on the task at hand.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def run(self, input_data: BackendInput) -> BackendOutput:
        """Implement backend functionality."""
        logger.info("Backend: implementing task '%s'", input_data.task_description[:60] + ("..." if len(input_data.task_description) > 60 else ""))
        context_parts = [
            f"**Task:** {input_data.task_description}",
            f"**Requirements:** {input_data.requirements}",
            f"**Language:** {input_data.language}",
        ]
        if input_data.architecture:
            context_parts.extend([
                "",
                "**Architecture:**",
                input_data.architecture.overview,
                *[f"- {c.name} ({c.type}): {c.technology}" for c in input_data.architecture.components if c.technology],
            ])
        if input_data.existing_code:
            context_parts.extend(["", "**Existing code:**", input_data.existing_code])
        if input_data.api_spec:
            context_parts.extend(["", "**API spec:**", input_data.api_spec])
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

        prompt = BACKEND_PROMPT + "\n\n---\n\n" + "\n".join(context_parts)
        data = self.llm.complete_json(prompt, temperature=0.2)

        code = data.get("code", "")
        if code and "\\n" in code:
            code = code.replace("\\n", "\n")
        tests = data.get("tests", "")
        if tests and "\\n" in tests:
            tests = tests.replace("\\n", "\n")

        summary = data.get("summary", "")
        logger.info("Backend: done, code=%s chars, summary=%s chars", len(code), len(summary))
        return BackendOutput(
            code=code,
            language=data.get("language", input_data.language),
            summary=summary,
            files=data.get("files", {}),
            tests=tests,
            suggested_commit_message=data.get("suggested_commit_message", ""),
        )
