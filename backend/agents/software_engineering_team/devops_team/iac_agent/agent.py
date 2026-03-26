"""Infrastructure as Code agent."""

from __future__ import annotations

from llm_service import LLMClient

from .models import IaCAgentInput, IaCAgentOutput
from .prompts import IAC_AGENT_PROMPT


class InfrastructureAsCodeAgent:
    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def run(self, input_data: IaCAgentInput) -> IaCAgentOutput:
        spec = input_data.task_spec
        context = (
            f"task_id={spec.task_id}\n"
            f"title={spec.title}\n"
            f"constraints={spec.constraints.model_dump()}\n"
            f"included={spec.scope.included}\n"
            f"excluded={spec.scope.excluded}\n"
            f"repo_summary={input_data.repo_summary}\n"
        )
        data = self.llm.complete_json(
            IAC_AGENT_PROMPT + "\n\n---\n\n" + context, temperature=0.1, think=True
        )
        return IaCAgentOutput(
            artifacts=data.get("artifacts") or {},
            summary=data.get("summary", ""),
            plan_summary=data.get("plan_summary", ""),
            destructive_changes_detected=bool(data.get("destructive_changes_detected", False)),
            blast_radius_notes=data.get("blast_radius_notes") or [],
        )
