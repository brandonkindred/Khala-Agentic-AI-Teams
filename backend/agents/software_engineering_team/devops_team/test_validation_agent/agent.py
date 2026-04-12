"""DevOps test and validation agent.

Built on the AWS Strands Agents SDK via ``llm_service.LLMClientModel``.
"""

from __future__ import annotations

import logging

from strands import Agent

from llm_service import LLMClient, LLMClientModel

from .models import (
    DevOpsTestValidationInput,
    DevOpsTestValidationOutput,
)
from .prompts import DEVOPS_TEST_VALIDATION_PROMPT

logger = logging.getLogger(__name__)


class DevOpsTestValidationAgent:
    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self._model = LLMClientModel(
            llm_client,
            agent_key="devops_test_validation",
            temperature=0.0,
            think=True,
        )

    def run(self, input_data: DevOpsTestValidationInput) -> DevOpsTestValidationOutput:
        user_prompt = (
            "Acting as a devops test validation agent, aggregate the tool "
            "results and determine which quality gates passed. Produce "
            "structured JSON with fields: approved, quality_gates (dict of "
            "gate name to pass|fail), acceptance_trace, evidence, summary.\n\n"
            f"acceptance_criteria={input_data.acceptance_criteria}\n"
            f"tool_results={input_data.tool_results}\n"
        )

        agent = Agent(model=self._model, system_prompt=DEVOPS_TEST_VALIDATION_PROMPT)

        try:
            agent_result = agent(user_prompt, structured_output_model=DevOpsTestValidationOutput)
            result = agent_result.structured_output
            if not isinstance(result, DevOpsTestValidationOutput):
                raise TypeError(
                    f"Expected DevOpsTestValidationOutput, "
                    f"got {type(result).__name__ if result else 'None'}"
                )
        except Exception as exc:  # noqa: BLE001 — LLM/validation failures must not crash the run
            logger.warning(
                "DevOpsTestValidation: structured_output failed (%s); returning empty", exc
            )
            return DevOpsTestValidationOutput(
                approved=False,
                quality_gates={},
                acceptance_trace=[],
                evidence=[],
                summary=f"Test validation failed: {exc}",
            )

        # Re-derive ``approved``: a single ``fail`` gate forces rejection.
        if any(v == "fail" for v in result.quality_gates.values()):
            result.approved = False
        return result
