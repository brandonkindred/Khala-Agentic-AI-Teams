"""DevOps test and validation agent."""

from __future__ import annotations

from software_engineering_team.shared.llm import LLMClient

from .models import (
    DevOpsTestValidationInput,
    DevOpsTestValidationOutput,
    ValidationEvidence,
)
from .prompts import DEVOPS_TEST_VALIDATION_PROMPT


class DevOpsTestValidationAgent:
    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def run(self, input_data: DevOpsTestValidationInput) -> DevOpsTestValidationOutput:
        context = (
            f"acceptance_criteria={input_data.acceptance_criteria}\n"
            f"tool_results={input_data.tool_results}\n"
        )
        data = self.llm.complete_json(DEVOPS_TEST_VALIDATION_PROMPT + "\n\n---\n\n" + context, temperature=0.0)
        gates = data.get("quality_gates") or {}
        approved = bool(data.get("approved", False))
        if any(v == "fail" for v in gates.values()):
            approved = False
        return DevOpsTestValidationOutput(
            approved=approved,
            quality_gates=gates,
            acceptance_trace=data.get("acceptance_trace") or [],
            evidence=[ValidationEvidence(**e) for e in (data.get("evidence") or []) if isinstance(e, dict)],
            summary=data.get("summary", ""),
        )
