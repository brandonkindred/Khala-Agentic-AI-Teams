"""Deployment strategy agent.

Built on the AWS Strands Agents SDK via ``llm_service.LLMClientModel``.
"""

from __future__ import annotations

import logging

from strands import Agent

from llm_service import LLMClient, LLMClientModel

from .models import DeploymentStrategyAgentInput, DeploymentStrategyAgentOutput
from .prompts import DEPLOYMENT_STRATEGY_PROMPT

logger = logging.getLogger(__name__)


class DeploymentStrategyAgent:
    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client
        self._model = LLMClientModel(
            llm_client,
            agent_key="deployment_strategy",
            temperature=0.1,
            think=True,
        )

    def run(self, input_data: DeploymentStrategyAgentInput) -> DeploymentStrategyAgentOutput:
        spec = input_data.task_spec
        user_prompt = (
            "Acting as a deployment strategy specialist, design the deployment "
            "and rollback plan for the task below. Produce structured JSON with "
            "fields: artifacts (deploy manifests), strategy, rollback_plan, "
            "health_checks, rollout_timeout_minutes, summary.\n\n"
            f"task_id={spec.task_id}\n"
            f"constraints={spec.constraints.model_dump()}\n"
            f"environments={spec.platform_scope.environments}\n"
            f"acceptance_criteria={spec.acceptance_criteria}\n"
            f"nfr={spec.non_functional_requirements}\n"
        )

        agent = Agent(model=self._model, system_prompt=DEPLOYMENT_STRATEGY_PROMPT)

        try:
            agent_result = agent(user_prompt, structured_output_model=DeploymentStrategyAgentOutput)
            result = agent_result.structured_output
            if not isinstance(result, DeploymentStrategyAgentOutput):
                raise TypeError(
                    f"Expected DeploymentStrategyAgentOutput, "
                    f"got {type(result).__name__ if result else 'None'}"
                )
        except Exception as exc:  # noqa: BLE001 — LLM/validation failures must not crash the run
            logger.warning(
                "DeploymentStrategy: structured_output failed (%s); returning empty output", exc
            )
            return DeploymentStrategyAgentOutput(
                artifacts={},
                strategy="",
                rollback_plan=[],
                health_checks=[],
                rollout_timeout_minutes=15,
                summary=f"Deployment strategy failed: {exc}",
            )

        return result
