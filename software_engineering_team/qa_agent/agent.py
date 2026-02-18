"""QA Expert agent: bug detection, integration tests, live testing."""

from __future__ import annotations

import logging
from typing import Any, Dict

from shared.llm import LLMClient

from .models import BugReport, QAInput, QAOutput
from .prompts import QA_PROMPT, QA_PROMPT_FIX_BUILD, QA_PROMPT_WRITE_TESTS

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
        logger.info(
            "QA: reviewing %s chars of code, mode=%s",
            len(input_data.code or ""),
            input_data.request_mode or "default",
        )
        base_prompt = QA_PROMPT
        if input_data.request_mode == "fix_build" and input_data.build_errors:
            base_prompt = QA_PROMPT + "\n\n" + QA_PROMPT_FIX_BUILD
        elif input_data.request_mode == "write_tests":
            base_prompt = QA_PROMPT + "\n\n" + QA_PROMPT_WRITE_TESTS

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
        if input_data.build_errors:
            context_parts.append(f"**Build/compiler errors:**\n```\n{input_data.build_errors}\n```")

        prompt = base_prompt + "\n\n---\n\n" + "\n".join(context_parts)
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
                        recommendation=b.get("recommendation", ""),
                    )
                )

        integration_tests = data.get("integration_tests", "")
        if integration_tests and "\\n" in integration_tests:
            integration_tests = integration_tests.replace("\\n", "\n")
        unit_tests = data.get("unit_tests", "")
        if unit_tests and "\\n" in unit_tests:
            unit_tests = unit_tests.replace("\\n", "\n")
        readme_content = data.get("readme_content", "")
        if readme_content and "\\n" in readme_content:
            readme_content = readme_content.replace("\\n", "\n")

        critical_bugs = [b for b in bugs if b.severity in ("critical", "high")]
        approved = len(critical_bugs) == 0

        logger.info("QA: done, %s issues found, approved=%s", len(bugs), approved)
        return QAOutput(
            bugs_found=bugs,
            approved=approved,
            integration_tests=integration_tests,
            unit_tests=unit_tests,
            test_plan=data.get("test_plan", ""),
            summary=data.get("summary", ""),
            live_test_notes=data.get("live_test_notes", ""),
            readme_content=readme_content,
            suggested_commit_message=data.get("suggested_commit_message", ""),
        )
