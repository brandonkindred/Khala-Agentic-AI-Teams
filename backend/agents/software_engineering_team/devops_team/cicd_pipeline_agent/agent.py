"""CI/CD pipeline agent.

Built on the AWS Strands Agents SDK via ``llm_service.LLMClientModel``.
"""

from __future__ import annotations

import logging

from strands import Agent

from llm_service import LLMClient, LLMClientModel

from .models import CICDPipelineAgentInput, CICDPipelineAgentOutput
from .prompts import CICD_PIPELINE_PROMPT

logger = logging.getLogger(__name__)


class CICDPipelineAgent:
    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client
        self._model = LLMClientModel(
            llm_client,
            agent_key="cicd_pipeline",
            temperature=0.1,
            think=True,
        )

    def run(self, input_data: CICDPipelineAgentInput) -> CICDPipelineAgentOutput:
        spec = input_data.task_spec
        user_prompt = (
            "Acting as a ci/cd pipeline specialist, design the build, test, "
            "scan, and deploy pipeline for the task below. Produce structured "
            "JSON with fields: artifacts (dict of workflow files), "
            "pipeline_job_graph_summary, required_gates_present, summary, risks.\n\n"
            f"task_id={spec.task_id}\n"
            f"title={spec.title}\n"
            f"environments={spec.platform_scope.environments}\n"
            f"constraints={spec.constraints.model_dump()}\n"
            f"acceptance_criteria={spec.acceptance_criteria}\n"
            f"existing_pipeline={input_data.existing_pipeline[:4000]}\n"
        )

        agent = Agent(model=self._model, system_prompt=CICD_PIPELINE_PROMPT)

        try:
            agent_result = agent(user_prompt, structured_output_model=CICDPipelineAgentOutput)
            result = agent_result.structured_output
            if not isinstance(result, CICDPipelineAgentOutput):
                raise TypeError(
                    f"Expected CICDPipelineAgentOutput, "
                    f"got {type(result).__name__ if result else 'None'}"
                )
        except Exception as exc:  # noqa: BLE001 — LLM/validation failures must not crash the run
            logger.warning("CICD: structured_output failed (%s); returning empty output", exc)
            return CICDPipelineAgentOutput(
                artifacts={},
                pipeline_job_graph_summary="",
                required_gates_present=False,
                summary=f"CICD generation failed: {exc}",
                risks=[],
            )

        return result
