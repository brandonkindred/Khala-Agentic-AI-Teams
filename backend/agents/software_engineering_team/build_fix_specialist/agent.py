"""Build Fix Specialist: produces minimal, targeted edits to fix build/test failures."""

from __future__ import annotations

import logging

from strands import Agent

from llm_service import get_strands_model
from llm_service.strands_model import resolve_strands_model
from shared.llm_recovery import agent_call_json

from .models import BuildFixInput, BuildFixOutput, parse_code_edits
from .prompts import BUILD_FIX_SPECIALIST_PROMPT

logger = logging.getLogger(__name__)


class BuildFixSpecialistAgent:
    """
    Specialist that produces minimal code edits to fix build or test failures.
    Used when full regeneration has failed 2+ times with the same error.
    """

    def __init__(self, llm_client=None) -> None:
        _model = resolve_strands_model(
            llm_client, agent_key="build_fix_specialist", get_strands_model_fn=get_strands_model
        )
        self._agent = Agent(model=_model, system_prompt=BUILD_FIX_SPECIALIST_PROMPT)

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

        prompt = "\n".join(context_parts)
        data = agent_call_json(self._agent, prompt, required_keys={"edits"})
        edits = parse_code_edits(data)

        summary = data.get("summary", "") if isinstance(data, dict) else ""
        logger.info(
            "BuildFixSpecialist: produced %d edits, summary=%s",
            len(edits),
            summary[:80] if summary else "",
        )
        return BuildFixOutput(edits=edits, summary=summary)
