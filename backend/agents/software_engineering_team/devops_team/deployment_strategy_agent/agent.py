"""Deployment strategy agent."""

from __future__ import annotations

from typing import Any, Dict

from software_engineering_team.devops_team._agent_template import DevOpsSingleShotAgent

from .models import DeploymentStrategyAgentInput, DeploymentStrategyAgentOutput
from .prompts import DEPLOYMENT_STRATEGY_PROMPT


class DeploymentStrategyAgent(DevOpsSingleShotAgent):
    """Produce deployment strategy artifacts via a single structured LLM call.

    Invariants: instance state is limited to ``llm`` and ``_model`` from the
    base; ``run`` is stateless across calls.
    """

    PROMPT = DEPLOYMENT_STRATEGY_PROMPT

    def build_context(self, input_data: DeploymentStrategyAgentInput) -> str:
        """Build the deployment prompt context from the task spec.

        Preconditions: ``input_data`` is a valid ``DeploymentStrategyAgentInput``.
        Postconditions: returns the same context string shape the pre-migration
        agent appended after the prompt separator.
        """
        spec = input_data.task_spec
        return (
            f"task_id={spec.task_id}\n"
            f"constraints={spec.constraints.model_dump()}\n"
            f"environments={spec.platform_scope.environments}\n"
            f"acceptance_criteria={spec.acceptance_criteria}\n"
            f"nfr={spec.non_functional_requirements}\n"
        )

    def build_output(
        self, input_data: DeploymentStrategyAgentInput, data: Dict[str, Any]
    ) -> DeploymentStrategyAgentOutput:
        """Map the LLM JSON dict onto ``DeploymentStrategyAgentOutput``.

        Preconditions: ``data`` is the dict from ``complete_json_with_continuation``.
        Postconditions: returns ``DeploymentStrategyAgentOutput`` with the same
        field defaults as the pre-migration agent, including
        ``rollout_timeout_minutes=int(data.get(..., 15) or 15)``.
        """
        return DeploymentStrategyAgentOutput(
            artifacts=data.get("artifacts") or {},
            strategy=data.get("strategy", ""),
            rollback_plan=data.get("rollback_plan") or [],
            health_checks=data.get("health_checks") or [],
            rollout_timeout_minutes=int(data.get("rollout_timeout_minutes", 15) or 15),
            summary=data.get("summary", ""),
        )
