"""Infrastructure Debug agent — classifies IaC execution errors.

Built on the AWS Strands Agents SDK via ``llm_service.LLMClientModel``.
"""

from __future__ import annotations

import logging

from strands import Agent

from llm_service import LLMClient, LLMClientModel

from .models import IaCDebugInput, IaCDebugOutput
from .prompts import INFRA_DEBUG_PROMPT

logger = logging.getLogger(__name__)

_FIXABLE_TYPES = frozenset({"syntax", "validation"})


class InfraDebugAgent:
    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self._model = LLMClientModel(
            llm_client,
            agent_key="infra_debug",
            temperature=0.1,
            think=True,
        )

    def run(self, input_data: IaCDebugInput) -> IaCDebugOutput:
        artifacts_snippet = ""
        for fname, content in list(input_data.artifacts.items())[:5]:
            artifacts_snippet += f"\n### {fname} ###\n{content[:2000]}\n"

        user_prompt = (
            "Acting as an infrastructure debug specialist, classify the "
            "execution errors below and determine whether they are fixable "
            "via code changes. Produce structured JSON with fields: errors "
            "(each with error_type, tool, file_path, line_number, "
            "error_message), summary, fixable.\n\n"
            f"Tool: {input_data.tool_name}\n"
            f"Command: {input_data.command}\n\n"
            f"--- Execution Output ---\n{input_data.execution_output[:4000]}\n\n"
            f"--- Artifacts ---\n{artifacts_snippet}\n"
        )

        agent = Agent(model=self._model, system_prompt=INFRA_DEBUG_PROMPT)

        try:
            agent_result = agent(user_prompt, structured_output_model=IaCDebugOutput)
            result = agent_result.structured_output
            if not isinstance(result, IaCDebugOutput):
                raise TypeError(
                    f"Expected IaCDebugOutput, got {type(result).__name__ if result else 'None'}"
                )
        except Exception as exc:  # noqa: BLE001 — LLM/validation failures must not crash the run
            logger.warning("InfraDebug: structured_output failed (%s); returning empty output", exc)
            return IaCDebugOutput(
                errors=[],
                summary=f"Infrastructure debug failed: {exc}",
                fixable=False,
            )

        # Fill in raw_output on each error and re-derive ``fixable``: an
        # error is fixable iff the LLM explicitly said so OR all classified
        # errors are syntax/validation (safe to patch in-code).
        for err in result.errors:
            if not err.tool:
                err.tool = input_data.tool_name
            if not err.raw_output:
                err.raw_output = input_data.execution_output[:500]

        if result.errors and not result.fixable:
            result.fixable = all(e.error_type in _FIXABLE_TYPES for e in result.errors)

        return result
