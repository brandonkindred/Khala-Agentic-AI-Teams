"""Acceptance Criteria Verifier agent.

Built on the AWS Strands Agents SDK via ``llm_service.get_strands_model``. The
model returned by ``get_strands_model`` is passed to a Strands ``Agent`` so the
agent inherits retries, per-agent model routing, telemetry, and the
dummy-client path for tests.
"""

from __future__ import annotations

import logging

from strands import Agent

from llm_service import get_strands_model

from .models import AcceptanceVerifierInput, AcceptanceVerifierOutput
from .prompts import ACCEPTANCE_VERIFIER_PROMPT

logger = logging.getLogger(__name__)


class AcceptanceVerifierAgent:
    """
    Verifies that delivered code satisfies each acceptance criterion.
    Returns per-criterion status with evidence.
    """

    def __init__(self, llm_client=None) -> None:
        from strands.models.model import Model as _StrandsModel
        if llm_client is not None and isinstance(llm_client, _StrandsModel):
            self._model = llm_client
        else:
            self._model = get_strands_model("acceptance_verifier")

    def run(self, input_data: AcceptanceVerifierInput) -> AcceptanceVerifierOutput:
        """Verify each acceptance criterion against the code."""
        # Short-circuit on empty criteria — avoids an unnecessary LLM round-trip.
        if not input_data.acceptance_criteria:
            return AcceptanceVerifierOutput(
                all_satisfied=True, per_criterion=[], summary="No criteria to verify"
            )

        logger.info(
            "AcceptanceVerifier: checking %s criteria against %s chars of code",
            len(input_data.acceptance_criteria),
            len(input_data.code or ""),
        )

        user_prompt = self._build_user_prompt(input_data)

        # A fresh Strands Agent per call — reusing the same instance across
        # calls breaks structured_output forced-tool-choice on the second
        # call (Strands accumulates message history).
        agent = Agent(model=self._model, system_prompt=ACCEPTANCE_VERIFIER_PROMPT)

        try:
            agent_result = agent(user_prompt, structured_output_model=AcceptanceVerifierOutput)
            result = agent_result.structured_output
            if not isinstance(result, AcceptanceVerifierOutput):
                raise TypeError(
                    f"Expected AcceptanceVerifierOutput, "
                    f"got {type(result).__name__ if result else 'None'}"
                )
        except Exception as exc:  # noqa: BLE001 — LLM/validation failures must not crash the run
            logger.warning(
                "AcceptanceVerifier: structured_output failed (%s); returning fallback", exc
            )
            return AcceptanceVerifierOutput(
                all_satisfied=False,
                per_criterion=[],
                summary=f"Acceptance verification failed: {exc}",
            )

        # Re-derive ``all_satisfied`` from the per-criterion list when present,
        # so a disagreement between the LLM's top-level flag and the detailed
        # statuses is resolved in favor of the detailed statuses.
        if result.per_criterion:
            result.all_satisfied = all(c.satisfied for c in result.per_criterion)

        logger.info(
            "AcceptanceVerifier: %s/%s satisfied, all_satisfied=%s",
            sum(1 for c in result.per_criterion if c.satisfied),
            len(result.per_criterion),
            result.all_satisfied,
        )
        return result

    @staticmethod
    def _build_user_prompt(input_data: AcceptanceVerifierInput) -> str:
        """Assemble the user-facing prompt.

        The persona (``ACCEPTANCE_VERIFIER_PROMPT``) lives on the Strands
        ``Agent``'s system prompt. The user prompt carries the code and
        the criteria to verify, plus an explicit schema hint. The phrases
        "acceptance criteria verifier" and "per_criterion" MUST appear
        here because ``DummyLLMClient.complete_json`` pattern-matches on
        them to return a deterministic stub in tests — see
        llm_service/README.md "Migration rule: keep pattern anchors in
        the user prompt".
        """
        parts = [
            "Acting as an acceptance criteria verifier, evaluate whether the "
            "delivered code satisfies each acceptance criterion below. Produce "
            "structured JSON with fields: all_satisfied, per_criterion, summary. "
            "Each per_criterion entry must include criterion, satisfied, and "
            "evidence.",
            "",
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
            parts.extend(["", "**Spec (excerpt):**", input_data.spec_content[:4000]])
        if input_data.architecture:
            parts.append(f"**Architecture:** {input_data.architecture.overview}")

        return "\n".join(parts)
