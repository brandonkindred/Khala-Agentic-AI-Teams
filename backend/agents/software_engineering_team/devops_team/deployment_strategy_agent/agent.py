"""Deployment strategy agent."""

from __future__ import annotations

import json

from strands import Agent

from llm_service import LLMClient, get_strands_model

from .models import DeploymentStrategyAgentInput, DeploymentStrategyAgentOutput
from .prompts import DEPLOYMENT_STRATEGY_PROMPT


class DeploymentStrategyAgent:
    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client
        from strands.models.model import Model as _StrandsModel
        if isinstance(llm_client, _StrandsModel):
            self._model = llm_client
        else:
            self._model = get_strands_model("devops")

    def run(self, input_data: DeploymentStrategyAgentInput) -> DeploymentStrategyAgentOutput:
        spec = input_data.task_spec
        context = (
            f"task_id={spec.task_id}\n"
            f"constraints={spec.constraints.model_dump()}\n"
            f"environments={spec.platform_scope.environments}\n"
            f"acceptance_criteria={spec.acceptance_criteria}\n"
            f"nfr={spec.non_functional_requirements}\n"
        )
        data = json.loads(str(Agent(model=self._model)(
            DEPLOYMENT_STRATEGY_PROMPT + "\n\n---\n\n" + context, temperature=0.1, think=True
        )).strip())
        return DeploymentStrategyAgentOutput(
            artifacts=data.get("artifacts") or {},
            strategy=data.get("strategy", ""),
            rollback_plan=data.get("rollback_plan") or [],
            health_checks=data.get("health_checks") or [],
            rollout_timeout_minutes=int(data.get("rollout_timeout_minutes", 15) or 15),
            summary=data.get("summary", ""),
        )
