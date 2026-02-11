"""QA Expert agent: bug detection, integration tests, live testing."""

from __future__ import annotations

import logging
from typing import Any, Dict

from shared.llm import LLMClient

from .models import BugReport, QAInput, QAOutput
from .prompts import QA_PROMPT

logger = logging.getLogger(__name__)


class QAExpertAgent:
    """
    QA expert that reviews code for bugs, fixes them, runs live testing,
    and ensures adequate integration tests.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def run(self, input_data: QAInput) -> QAOutput:
        """Review code, fix bugs, and produce integration tests."""
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
        if input_data.run_instructions:
            context_parts.append(f"**Run instructions:** {input_data.run_instructions}")

        prompt = QA_PROMPT + "\n\n---\n\n" + "\n".join(context_parts)
        data = self.llm.complete_json(prompt, temperature=0.1)

        bugs = []
        for b in data.get("bugs_found") or []:
            if isinstance(b, dict) and b.get("description"):
                bugs.append(
                    BugReport(
                        severity=b.get("severity", "medium"),
                        description=b["description"],
                        location=b.get("location", ""),
                        steps_to_reproduce=b.get("steps_to_reproduce", ""),
                        expected_vs_actual=b.get("expected_vs_actual", ""),
                    )
                )

        fixed_code = data.get("fixed_code", input_data.code)
        if fixed_code and "\\n" in fixed_code:
            fixed_code = fixed_code.replace("\\n", "\n")
        integration_tests = data.get("integration_tests", "")
        if integration_tests and "\\n" in integration_tests:
            integration_tests = integration_tests.replace("\\n", "\n")
        unit_tests = data.get("unit_tests", "")
        if unit_tests and "\\n" in unit_tests:
            unit_tests = unit_tests.replace("\\n", "\n")
        readme_content = data.get("readme_content", "")
        if readme_content and "\\n" in readme_content:
            readme_content = readme_content.replace("\\n", "\n")

        return QAOutput(
            bugs_found=bugs,
            fixed_code=fixed_code,
            integration_tests=integration_tests,
            unit_tests=unit_tests,
            test_plan=data.get("test_plan", ""),
            summary=data.get("summary", ""),
            live_test_notes=data.get("live_test_notes", ""),
            readme_content=readme_content,
            suggested_commit_message=data.get("suggested_commit_message", ""),
        )
