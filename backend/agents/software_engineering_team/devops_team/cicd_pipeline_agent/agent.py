"""CI/CD pipeline agent."""

from __future__ import annotations

from typing import Any, Dict

from software_engineering_team.devops_team._agent_template import DevOpsSingleShotAgent

from .models import CICDPipelineAgentInput, CICDPipelineAgentOutput
from .prompts import CICD_PIPELINE_PROMPT


class CICDPipelineAgent(DevOpsSingleShotAgent):
    """Produce CI/CD pipeline artifacts via a single structured LLM call.

    Invariants: instance state is limited to ``llm`` and ``_model`` from the
    base; ``run`` is stateless across calls.
    """

    PROMPT = CICD_PIPELINE_PROMPT

    def build_context(self, input_data: CICDPipelineAgentInput) -> str:
        """Build the CI/CD prompt context from the task spec and existing pipeline.

        Preconditions: ``input_data`` is a valid ``CICDPipelineAgentInput``.
        Postconditions: returns the pre-migration agent's context string shape
        plus an ``excluded`` line carrying ``spec.scope.excluded`` -- this
        agent otherwise never sees ``task.out_of_scope`` (only
        ``InfrastructureAsCodeAgent.build_context`` reads ``spec.scope``
        directly), so a pipeline artifact could otherwise violate an explicit
        exclusion (e.g. "do not touch the legacy Jenkins pipeline") with no
        specialist ever having been told about it.
        """
        spec = input_data.task_spec
        return (
            f"task_id={spec.task_id}\n"
            f"title={spec.title}\n"
            f"environments={spec.platform_scope.environments}\n"
            f"constraints={spec.constraints.model_dump()}\n"
            f"acceptance_criteria={spec.acceptance_criteria}\n"
            f"excluded={spec.scope.excluded}\n"
            f"existing_pipeline={input_data.existing_pipeline}\n"
        )

    def build_output(
        self, input_data: CICDPipelineAgentInput, data: Dict[str, Any]
    ) -> CICDPipelineAgentOutput:
        """Map the LLM JSON dict onto ``CICDPipelineAgentOutput``.

        Preconditions: ``data`` is the dict from ``complete_json_with_continuation``.
        Postconditions: returns ``CICDPipelineAgentOutput`` with the same field
        defaults as the pre-migration agent.
        """
        return CICDPipelineAgentOutput(
            artifacts=data.get("artifacts") or {},
            pipeline_job_graph_summary=data.get("pipeline_job_graph_summary", ""),
            required_gates_present=bool(data.get("required_gates_present", False)),
            summary=data.get("summary", ""),
            risks=data.get("risks") or [],
        )
