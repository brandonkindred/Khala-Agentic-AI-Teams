"""Infrastructure as Code agent.

Built on the AWS Strands Agents SDK via ``llm_service.LLMClientModel``.
"""

from __future__ import annotations

import logging

from strands import Agent

from llm_service import LLMClient, LLMClientModel

from .models import IaCAgentInput, IaCAgentOutput
from .prompts import IAC_AGENT_PROMPT

logger = logging.getLogger(__name__)


class InfrastructureAsCodeAgent:
    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client
        self._model = LLMClientModel(
            llm_client,
            agent_key="iac",
            temperature=0.1,
            think=True,
        )

    def run(self, input_data: IaCAgentInput) -> IaCAgentOutput:
        spec = input_data.task_spec
        user_prompt = (
            "Acting as an infrastructure as code (iac) specialist, generate the "
            "required terraform / cdk / helm artifacts for the task below. "
            "Produce structured JSON with fields: artifacts (dict of file path "
            "to content), summary, plan_summary, destructive_changes_detected, "
            "blast_radius_notes.\n\n"
            f"task_id={spec.task_id}\n"
            f"title={spec.title}\n"
            f"constraints={spec.constraints.model_dump()}\n"
            f"included={spec.scope.included}\n"
            f"excluded={spec.scope.excluded}\n"
            f"repo_summary={input_data.repo_summary}\n"
        )

        # Fresh Strands Agent per call — see comments on other Wave 1–3
        # migrations for why this is required.
        agent = Agent(model=self._model, system_prompt=IAC_AGENT_PROMPT)

        try:
            agent_result = agent(user_prompt, structured_output_model=IaCAgentOutput)
            result = agent_result.structured_output
            if not isinstance(result, IaCAgentOutput):
                raise TypeError(
                    f"Expected IaCAgentOutput, got {type(result).__name__ if result else 'None'}"
                )
        except Exception as exc:  # noqa: BLE001 — LLM/validation failures must not crash the run
            logger.warning("IaC: structured_output failed (%s); returning empty output", exc)
            return IaCAgentOutput(
                artifacts={},
                summary=f"IaC generation failed: {exc}",
                plan_summary="",
                destructive_changes_detected=False,
                blast_radius_notes=[],
            )

        return result
