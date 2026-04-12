"""Change review agent.

Built on the AWS Strands Agents SDK via ``llm_service.LLMClientModel``.
"""

from __future__ import annotations

import logging

from devops_team.models import ReviewFinding
from strands import Agent

from llm_service import LLMClient, LLMClientModel

from .models import ChangeReviewInput, ChangeReviewOutput
from .prompts import CHANGE_REVIEW_PROMPT

logger = logging.getLogger(__name__)


class ChangeReviewAgent:
    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self._model = LLMClientModel(
            llm_client,
            agent_key="change_review",
            temperature=0.0,
            think=True,
        )

    def run(self, input_data: ChangeReviewInput) -> ChangeReviewOutput:
        user_prompt = (
            "Acting as an expert devops change reviewer, audit the proposed "
            "artifacts and produce structured JSON with fields: approved, "
            "findings (each with severity, blocking, message), summary.\n\n"
            f"task={input_data.task_description}\n"
            f"artifacts={list(input_data.artifacts.keys())}\n"
        )

        agent = Agent(model=self._model, system_prompt=CHANGE_REVIEW_PROMPT)

        try:
            agent_result = agent(user_prompt, structured_output_model=ChangeReviewOutput)
            result = agent_result.structured_output
            if not isinstance(result, ChangeReviewOutput):
                raise TypeError(
                    f"Expected ChangeReviewOutput, "
                    f"got {type(result).__name__ if result else 'None'}"
                )
        except Exception as exc:  # noqa: BLE001 — LLM/validation failures must not crash the run
            logger.warning("ChangeReview: structured_output failed (%s); returning empty", exc)
            return ChangeReviewOutput(
                approved=False,
                findings=[],
                summary=f"Change review failed: {exc}",
            )

        # Re-derive ``approved`` from findings: a single blocking finding
        # overrides any approved=True flag the LLM returned. This is policy.
        blocking = any(isinstance(f, ReviewFinding) and f.blocking for f in result.findings)
        result.approved = result.approved and not blocking
        return result
