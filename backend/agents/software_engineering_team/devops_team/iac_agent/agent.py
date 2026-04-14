"""Infrastructure as Code agent."""

from __future__ import annotations

import json

from strands import Agent

from llm_service import LLMClient, get_strands_model

from .models import IaCAgentInput, IaCAgentOutput
from .prompts import IAC_AGENT_PROMPT


class InfrastructureAsCodeAgent:
    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client
        from strands.models.model import Model as _StrandsModel
        if isinstance(llm_client, _StrandsModel):
            self._model = llm_client
        else:
            self._model = get_strands_model("devops")

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
        data = json.loads(str(Agent(model=self._model)(
            IAC_AGENT_PROMPT + "\n\n---\n\n" + context, temperature=0.1, think=True
        )).strip())
        return IaCAgentOutput(
            artifacts=data.get("artifacts") or {},
            summary=data.get("summary", ""),
            plan_summary=data.get("plan_summary", ""),
            destructive_changes_detected=bool(data.get("destructive_changes_detected", False)),
            blast_radius_notes=data.get("blast_radius_notes") or [],
        )
