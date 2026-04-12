"""DevSecOps review agent.

Built on the AWS Strands Agents SDK via ``llm_service.LLMClientModel``.
"""

from __future__ import annotations

import logging

from devops_team.models import ReviewFinding
from strands import Agent

from llm_service import LLMClient, LLMClientModel

from .models import DevSecOpsReviewInput, DevSecOpsReviewOutput
from .prompts import DEVSECOPS_REVIEW_PROMPT

logger = logging.getLogger(__name__)


class DevSecOpsReviewAgent:
    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self._model = LLMClientModel(
            llm_client,
            agent_key="devsecops_review",
            temperature=0.0,
            think=True,
        )

    def run(self, input_data: DevSecOpsReviewInput) -> DevSecOpsReviewOutput:
        user_prompt = (
            "Acting as a devsecops reviewer, audit the proposed artifacts for "
            "security and compliance concerns. Produce structured JSON with "
            "fields: approved, findings (each with severity, blocking, "
            "message), summary.\n\n"
            f"task={input_data.task_description}\n"
            f"requirements={input_data.requirements}\n"
            f"artifacts={list(input_data.artifacts.keys())}\n"
        )

        agent = Agent(model=self._model, system_prompt=DEVSECOPS_REVIEW_PROMPT)

        try:
            agent_result = agent(user_prompt, structured_output_model=DevSecOpsReviewOutput)
            result = agent_result.structured_output
            if not isinstance(result, DevSecOpsReviewOutput):
                raise TypeError(
                    f"Expected DevSecOpsReviewOutput, "
                    f"got {type(result).__name__ if result else 'None'}"
                )
        except Exception as exc:  # noqa: BLE001 — LLM/validation failures must not crash the run
            logger.warning("DevSecOps: structured_output failed (%s); returning empty", exc)
            return DevSecOpsReviewOutput(
                approved=False,
                findings=[],
                summary=f"DevSecOps review failed: {exc}",
            )

        # Re-derive ``approved``: any blocking finding or critical/high
        # severity forces a reject. This is policy and lives in the agent.
        blocking = any(
            isinstance(f, ReviewFinding) and (f.blocking or f.severity in ("critical", "high"))
            for f in result.findings
        )
        result.approved = result.approved and not blocking
        return result
