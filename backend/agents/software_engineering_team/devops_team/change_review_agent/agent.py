"""Change review agent."""

from __future__ import annotations

import json

from devops_team.models import ReviewFinding
from strands import Agent

from llm_service import LLMClient, get_strands_model

from .models import ChangeReviewInput, ChangeReviewOutput
from .prompts import CHANGE_REVIEW_PROMPT


class ChangeReviewAgent:
    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client
        from strands.models.model import Model as _StrandsModel
        if isinstance(llm_client, _StrandsModel):
            self._model = llm_client
        else:
            self._model = get_strands_model("devops")

    def run(self, input_data: ChangeReviewInput) -> ChangeReviewOutput:
        context = (
            f"task={input_data.task_description}\nartifacts={list(input_data.artifacts.keys())}\n"
        )
        data = json.loads(str(Agent(model=self._model)(
            CHANGE_REVIEW_PROMPT + "\n\n---\n\n" + context, temperature=0.0, think=True
        )).strip())
        findings = [ReviewFinding(**f) for f in (data.get("findings") or []) if isinstance(f, dict)]
        blocking = any(f.blocking for f in findings)
        return ChangeReviewOutput(
            approved=bool(data.get("approved", not blocking)) and not blocking,
            findings=findings,
            summary=data.get("summary", ""),
        )
