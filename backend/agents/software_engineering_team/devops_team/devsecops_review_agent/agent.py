"""DevSecOps review agent."""

from __future__ import annotations

import json

from devops_team.models import ReviewFinding
from strands import Agent

from llm_service import LLMClient

from .models import DevSecOpsReviewInput, DevSecOpsReviewOutput
from .prompts import DEVSECOPS_REVIEW_PROMPT


class DevSecOpsReviewAgent:
    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client
        self._model = llm_client

    def run(self, input_data: DevSecOpsReviewInput) -> DevSecOpsReviewOutput:
        context = (
            f"task={input_data.task_description}\n"
            f"requirements={input_data.requirements}\n"
            f"artifacts={list(input_data.artifacts.keys())}\n"
        )
        data = json.loads(str(Agent(model=self._model)(
            DEVSECOPS_REVIEW_PROMPT + "\n\n---\n\n" + context, temperature=0.0, think=True
        )).strip())
        findings = [ReviewFinding(**f) for f in (data.get("findings") or []) if isinstance(f, dict)]
        blocking = any(f.blocking or f.severity in ("critical", "high") for f in findings)
        approved = bool(data.get("approved", not blocking)) and not blocking
        return DevSecOpsReviewOutput(
            approved=approved,
            findings=findings,
            summary=data.get("summary", ""),
        )
