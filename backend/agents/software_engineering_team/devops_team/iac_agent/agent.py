"""Infrastructure as Code agent."""

from __future__ import annotations

from typing import Any, Dict

from software_engineering_team.devops_team._agent_template import DevOpsSingleShotAgent
from software_engineering_team.devops_team._llm_cache import clear_cache

from .models import IaCAgentInput, IaCAgentOutput
from .prompts import IAC_AGENT_PROMPT


class InfrastructureAsCodeAgent(DevOpsSingleShotAgent):
    """Produce IaC artifacts for a devops task via a single structured LLM call.

    Invariants: instance state is limited to ``llm`` and ``_model`` from the
    base; ``run`` is stateless across calls.
    """

    PROMPT = IAC_AGENT_PROMPT
    CACHE_NAMESPACE = "devops:iac:v1"
    CACHE_ENV_VAR = "DEVOPS_IAC_CACHE_SIZE"
    OUTPUT_MODEL = IaCAgentOutput

    def build_context(self, input_data: IaCAgentInput) -> str:
        """Build the IaC prompt context from the task spec and repo summary.

        Preconditions: ``input_data`` is a valid ``IaCAgentInput``.
        Postconditions: returns the same context string shape the pre-migration
        agent appended after the prompt separator.
        """
        spec = input_data.task_spec
        return (
            f"task_id={spec.task_id}\n"
            f"title={spec.title}\n"
            f"constraints={spec.constraints.model_dump()}\n"
            f"included={spec.scope.included}\n"
            f"excluded={spec.scope.excluded}\n"
            f"repo_summary={input_data.repo_summary}\n"
        )

    def build_output(self, input_data: IaCAgentInput, data: Dict[str, Any]) -> IaCAgentOutput:
        """Map the LLM JSON dict onto ``IaCAgentOutput``.

        Preconditions: ``data`` is the dict from ``complete_json_with_continuation``.
        Postconditions: returns ``IaCAgentOutput`` with the same field defaults as
        the pre-migration agent (``artifacts``/``blast_radius_notes`` empty
        collections, empty string summaries, bool destructive flag).
        """
        return IaCAgentOutput(
            artifacts=data.get("artifacts") or {},
            summary=data.get("summary", ""),
            plan_summary=data.get("plan_summary", ""),
            destructive_changes_detected=bool(data.get("destructive_changes_detected", False)),
            blast_radius_notes=data.get("blast_radius_notes") or [],
        )


def clear_iac_cache() -> None:
    """Drop every cached IaC agent result. Intended for test teardown."""
    clear_cache(InfrastructureAsCodeAgent.CACHE_NAMESPACE, log_prefix="IaCAgent")
