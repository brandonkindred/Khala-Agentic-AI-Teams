"""Deployment strategy agent."""

from __future__ import annotations

from typing import Any, Dict

from software_engineering_team.devops_team._agent_template import DevOpsSingleShotAgent, _as_bool

from .models import DeploymentStrategyAgentInput, DeploymentStrategyAgentOutput
from .prompts import DEPLOYMENT_STRATEGY_PROMPT


class DeploymentStrategyAgent(DevOpsSingleShotAgent):
    """Produce deployment strategy artifacts via a single structured LLM call.

    Invariants: instance state is limited to ``llm`` and ``_model`` from the
    base. ``run`` is deterministic for identical inputs and the resolved
    model: repeated identical calls may return a cached result and skip the
    LLM. Cache reads/writes are fail-open and gated by ``CACHE_ENV_VAR``.
    """

    PROMPT = DEPLOYMENT_STRATEGY_PROMPT
    CACHE_NAMESPACE = "devops:deploy_strategy:v1"
    CACHE_ENV_VAR = "DEVOPS_DEPLOYMENT_STRATEGY_CACHE_SIZE"
    OUTPUT_MODEL = DeploymentStrategyAgentOutput

    def build_context(self, input_data: DeploymentStrategyAgentInput) -> str:
        """Build the deployment prompt context from the task spec.

        Preconditions: ``input_data`` is a valid ``DeploymentStrategyAgentInput``.
        Postconditions: returns the pre-migration agent's context string shape
        plus an ``excluded`` line carrying ``spec.scope.excluded`` -- this
        agent otherwise never sees ``task.out_of_scope`` (only
        ``InfrastructureAsCodeAgent.build_context`` reads ``spec.scope``
        directly), so a rollout strategy could otherwise violate an explicit
        exclusion (e.g. "do not use blue-green for this service") with no
        specialist ever having been told about it.
        """
        spec = input_data.task_spec
        return (
            f"task_id={spec.task_id}\n"
            f"constraints={spec.constraints.model_dump()}\n"
            f"environments={spec.platform_scope.environments}\n"
            f"acceptance_criteria={spec.acceptance_criteria}\n"
            f"excluded={spec.scope.excluded}\n"
            f"nfr={spec.non_functional_requirements}\n"
        )

    def build_output(
        self, input_data: DeploymentStrategyAgentInput, data: Dict[str, Any]
    ) -> DeploymentStrategyAgentOutput:
        """Map the LLM JSON dict onto ``DeploymentStrategyAgentOutput``.

        Preconditions: ``data`` is the dict from ``complete_json_with_continuation``.
        Postconditions: returns ``DeploymentStrategyAgentOutput`` with the same
        field defaults as the pre-migration agent, including
        ``rollout_timeout_minutes=int(data.get(..., 15) or 15)`` and
        ``alerting_configured=_as_bool(data.get("alerting_configured"))``
        (absent / ``"false"`` / other non-true values → False).
        """
        return DeploymentStrategyAgentOutput(
            artifacts=data.get("artifacts") or {},
            strategy=data.get("strategy", ""),
            rollback_plan=data.get("rollback_plan") or [],
            health_checks=data.get("health_checks") or [],
            rollout_timeout_minutes=int(data.get("rollout_timeout_minutes", 15) or 15),
            alerting_configured=_as_bool(data.get("alerting_configured")),
            summary=data.get("summary", ""),
        )


def clear_review_cache() -> None:
    """Drop every cached deployment strategy agent result. Intended for test teardown."""
    DeploymentStrategyAgent.clear_cache()
