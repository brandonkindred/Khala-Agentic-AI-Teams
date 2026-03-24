"""Build Fix Specialist: produces minimal, targeted edits to fix build/test failures."""

from __future__ import annotations

import logging
from typing import List

from llm_service import LLMClient

from .models import BuildFixInput, BuildFixOutput, CodeEdit
from .prompts import BUILD_FIX_SPECIALIST_PROMPT

logger = logging.getLogger(__name__)


class BuildFixSpecialistAgent:
    """
    Specialist that produces minimal code edits to fix build or test failures.
    Used when full regeneration has failed 2+ times with the same error.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def run(self, input_data: BuildFixInput) -> BuildFixOutput:
        """Produce minimal edits to fix the build error."""
        logger.info(
            "BuildFixSpecialist: analyzing %d chars of build errors, %d chars of affected code",
            len(input_data.build_errors or ""),
            len(input_data.affected_files_code or ""),
        )

        context_parts = [
            "**Build/compiler errors:**",
            "```",
            input_data.build_errors,
            "```",
            "",
            "**Affected files (current code):**",
            "```",
            input_data.affected_files_code,
            "```",
        ]
        if input_data.failing_test_content:
            context_parts.extend(
                [
                    "",
                    "**Failing test file content:**",
                    "```",
                    input_data.failing_test_content[:4000]
                    + ("..." if len(input_data.failing_test_content or "") > 4000 else ""),
                    "```",
                ]
            )
        if input_data.task_description:
            context_parts.insert(0, f"**Task context:** {input_data.task_description}\n")

        prompt = BUILD_FIX_SPECIALIST_PROMPT + "\n\n---\n\n" + "\n".join(context_parts)
        data = self.llm.complete_json(prompt, temperature=0.0)

        edits: List[CodeEdit] = []
        for e in data.get("edits") or []:
            if isinstance(e, dict) and e.get("file_path") and "old_text" in e and "new_text" in e:
                edits.append(
                    CodeEdit(
                        file_path=e["file_path"],
                        line_start=e.get("line_start"),
                        line_end=e.get("line_end"),
                        old_text=e["old_text"],
                        new_text=e["new_text"],
                    )
                )

        summary = data.get("summary", "")
        logger.info(
            "BuildFixSpecialist: produced %d edits, summary=%s",
            len(edits),
            summary[:80] if summary else "",
        )
        return BuildFixOutput(edits=edits, summary=summary)
