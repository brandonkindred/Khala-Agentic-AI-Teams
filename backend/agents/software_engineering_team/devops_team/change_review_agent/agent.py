"""Change review agent."""

from __future__ import annotations

from software_engineering_team.shared.llm import LLMClient

from devops_team.models import ReviewFinding

from .models import ChangeReviewInput, ChangeReviewOutput
from .prompts import CHANGE_REVIEW_PROMPT


class ChangeReviewAgent:
    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def run(self, input_data: ChangeReviewInput) -> ChangeReviewOutput:
        context = f"task={input_data.task_description}\nartifacts={list(input_data.artifacts.keys())}\n"
        data = self.llm.complete_json(CHANGE_REVIEW_PROMPT + "\n\n---\n\n" + context, temperature=0.0)
        findings = [ReviewFinding(**f) for f in (data.get("findings") or []) if isinstance(f, dict)]
        blocking = any(f.blocking for f in findings)
        return ChangeReviewOutput(
            approved=bool(data.get("approved", not blocking)) and not blocking,
            findings=findings,
            summary=data.get("summary", ""),
        )
