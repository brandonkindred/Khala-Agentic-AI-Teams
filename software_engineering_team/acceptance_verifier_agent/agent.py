"""Acceptance Criteria Verifier agent."""

from __future__ import annotations

import logging
from typing import List

from shared.llm import LLMClient

from .models import AcceptanceVerifierInput, AcceptanceVerifierOutput, CriterionStatus
from .prompts import ACCEPTANCE_VERIFIER_PROMPT

logger = logging.getLogger(__name__)


class AcceptanceVerifierAgent:
    """
    Verifies that delivered code satisfies each acceptance criterion.
    Returns per-criterion status with evidence.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def run(self, input_data: AcceptanceVerifierInput) -> AcceptanceVerifierOutput:
        """Verify each acceptance criterion against the code."""
        if not input_data.acceptance_criteria:
            return AcceptanceVerifierOutput(all_satisfied=True, per_criterion=[], summary="No criteria to verify")

        logger.info(
            "AcceptanceVerifier: checking %s criteria against %s chars of code",
            len(input_data.acceptance_criteria),
            len(input_data.code or ""),
        )

        context_parts = [
            f"**Language:** {input_data.language}",
            f"**Task description:** {input_data.task_description}",
            "**Acceptance criteria:**",
            *[f"- {c}" for c in input_data.acceptance_criteria],
            "**Code to verify:**",
            "```",
            input_data.code or "# No code",
            "```",
        ]
        if input_data.spec_content:
            context_parts.extend(["", "**Spec (excerpt):**", input_data.spec_content[:4000]])
        if input_data.architecture:
            context_parts.append(f"**Architecture:** {input_data.architecture.overview}")

        prompt = ACCEPTANCE_VERIFIER_PROMPT + "\n\n---\n\n" + "\n".join(context_parts)
        data = self.llm.complete_json(prompt, temperature=0.1)

        per_criterion: List[CriterionStatus] = []
        for item in data.get("per_criterion") or []:
            if isinstance(item, dict) and item.get("criterion") is not None:
                per_criterion.append(
                    CriterionStatus(
                        criterion=str(item["criterion"]),
                        satisfied=bool(item.get("satisfied", False)),
                        evidence=str(item.get("evidence", "")),
                    )
                )

        all_satisfied = data.get("all_satisfied", True)
        if per_criterion:
            all_satisfied = all(c.satisfied for c in per_criterion)

        logger.info(
            "AcceptanceVerifier: %s/%s satisfied, all_satisfied=%s",
            sum(1 for c in per_criterion if c.satisfied),
            len(per_criterion),
            all_satisfied,
        )
        return AcceptanceVerifierOutput(
            all_satisfied=all_satisfied,
            per_criterion=per_criterion,
            summary=data.get("summary", ""),
        )
