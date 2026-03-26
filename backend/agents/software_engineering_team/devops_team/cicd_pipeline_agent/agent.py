"""CI/CD pipeline agent."""

from __future__ import annotations

from llm_service import LLMClient

from .models import CICDPipelineAgentInput, CICDPipelineAgentOutput
from .prompts import CICD_PIPELINE_PROMPT


class CICDPipelineAgent:
    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def run(self, input_data: CICDPipelineAgentInput) -> CICDPipelineAgentOutput:
        spec = input_data.task_spec
        context = (
            f"task_id={spec.task_id}\n"
            f"title={spec.title}\n"
            f"environments={spec.platform_scope.environments}\n"
            f"constraints={spec.constraints.model_dump()}\n"
            f"acceptance_criteria={spec.acceptance_criteria}\n"
            f"existing_pipeline={input_data.existing_pipeline[:4000]}\n"
        )
        data = self.llm.complete_json(
            CICD_PIPELINE_PROMPT + "\n\n---\n\n" + context, temperature=0.1, think=True
        )
        return CICDPipelineAgentOutput(
            artifacts=data.get("artifacts") or {},
            pipeline_job_graph_summary=data.get("pipeline_job_graph_summary", ""),
            required_gates_present=bool(data.get("required_gates_present", False)),
            summary=data.get("summary", ""),
            risks=data.get("risks") or [],
        )
