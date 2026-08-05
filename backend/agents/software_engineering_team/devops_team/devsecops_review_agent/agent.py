"""DevSecOps review agent."""

from __future__ import annotations

import logging

from llm_service import LLMClient
from software_engineering_team.shared.security_service import derive_approved
from software_engineering_team.shared.single_shot_review import run_single_shot_review

from .models import DevSecOpsReviewInput, DevSecOpsReviewLLMResponse, DevSecOpsReviewOutput
from .prompts import DEVSECOPS_REVIEW_PROMPT

logger = logging.getLogger(__name__)


class DevSecOpsReviewAgent:
    """Infra security reviewer for DevOps artifacts (IAM/secrets/network).

    Invariants: instance state is limited to the injectable ``llm`` client;
    ``run`` is stateless across calls.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        """Store the review client.

        Preconditions: ``llm_client`` is not None (an ``LLMClient``).
        Postconditions: ``self.llm`` is the stored client, resolved fresh on
        every ``run`` call by ``run_single_shot_review``.
        """
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def run(self, input_data: DevSecOpsReviewInput) -> DevSecOpsReviewOutput:
        """Review DevOps artifacts and derive a blocking decision.

        Preconditions:
            ``input_data`` is a ``DevSecOpsReviewInput``.
        Postconditions:
            Returns a ``DevSecOpsReviewOutput`` whose ``approved`` follows the
            unified rule (:func:`derive_approved`): any blocking finding
            (critical/high severity or an explicit ``blocking`` flag) forces
            ``approved=False``; otherwise the model's ``approved`` is honored. An
            ``approved`` value that is present but null is treated as an explicit
            non-approval (fail closed), matching the legacy contract; an absent
            key defers entirely to the finding-derived default. On any
            model/validation failure surviving ``run_single_shot_review``'s
            corrective retry, returns a safe fallback with ``approved=False``, no
            findings, and a diagnostic summary. Never raises.
        """
        context = (
            f"task={input_data.task_description}\n"
            f"requirements={input_data.requirements}\n"
            f"artifacts={list(input_data.artifacts.keys())}\n"
        )
        try:
            response = run_single_shot_review(
                self.llm,
                agent_key="devops",
                prompt=DEVSECOPS_REVIEW_PROMPT + "\n\n---\n\n" + context,
                system_prompt="You are DevSecOpsReviewAgent, an infra security reviewer.",
                schema=DevSecOpsReviewLLMResponse,
                temperature=0.0,
                think=True,
            )
        except Exception as exc:  # noqa: BLE001 — LLM/validation failures must not crash the run
            logger.warning("DevSecOps: review failed (%s); returning fallback", exc)
            return DevSecOpsReviewOutput(
                approved=False,
                findings=[],
                summary=f"DevSecOps review failed: {exc}",
            )

        # Distinguish an absent ``approved`` key (no opinion -> defer to findings)
        # from a present-but-null value (an explicit non-approval -> fail closed).
        llm_approved = bool(response.approved) if "approved" in response.model_fields_set else None
        approved = derive_approved(response.findings, llm_approved=llm_approved)
        return DevSecOpsReviewOutput(
            approved=approved,
            findings=response.findings,
            summary=response.summary,
        )
