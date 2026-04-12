"""Accessibility Expert agent: WCAG 2.2 compliance review.

Built on the AWS Strands Agents SDK via ``llm_service.get_strands_model``. The
model returned by ``get_strands_model`` is passed to a Strands ``Agent`` so the
agent inherits retries, per-agent model routing, telemetry, and the
dummy-client path for tests.
"""

from __future__ import annotations

import logging

from strands import Agent

from llm_service import get_strands_model

from .models import AccessibilityInput, AccessibilityOutput
from .prompts import ACCESSIBILITY_PROMPT

logger = logging.getLogger(__name__)


class AccessibilityExpertAgent:
    """
    Accessibility expert that reviews frontend code for WCAG 2.2 compliance
    and produces a list of issues for the coding agent to fix.
    """

    def __init__(self, llm_client=None) -> None:
        if llm_client is not None:
            self._model = llm_client
        else:
            self._model = get_strands_model("accessibility")

    def run(self, input_data: AccessibilityInput) -> AccessibilityOutput:
        """Review code for WCAG 2.2 compliance and produce issue list."""
        logger.info("Accessibility: reviewing %s chars of code", len(input_data.code or ""))

        user_prompt = self._build_user_prompt(input_data)

        # A fresh Strands Agent per call — reusing the same instance across
        # calls breaks structured_output forced-tool-choice on the second
        # call (Strands accumulates message history).
        agent = Agent(model=self._model, system_prompt=ACCESSIBILITY_PROMPT)

        try:
            agent_result = agent(user_prompt, structured_output_model=AccessibilityOutput)
            result = agent_result.structured_output
            if not isinstance(result, AccessibilityOutput):
                raise TypeError(
                    f"Expected AccessibilityOutput, got {type(result).__name__ if result else 'None'}"
                )
        except Exception as exc:  # noqa: BLE001 — LLM/validation failures must not crash the run
            logger.warning("Accessibility: structured_output failed (%s); returning fallback", exc)
            return AccessibilityOutput(
                issues=[],
                approved=False,
                summary=f"Accessibility analysis failed: {exc}",
            )

        # Re-derive ``approved`` from severities so a disagreement between the
        # LLM's ``approved`` flag and the reported issue list is resolved in
        # favor of the issue list.
        critical_or_high = [i for i in result.issues if i.severity in ("critical", "high")]
        result.approved = len(critical_or_high) == 0

        logger.info(
            "Accessibility: done, %s issues found, approved=%s",
            len(result.issues),
            result.approved,
        )
        return result

    @staticmethod
    def _build_user_prompt(input_data: AccessibilityInput) -> str:
        """Assemble the user-facing prompt.

        The persona (``ACCESSIBILITY_PROMPT``) lives on the Strands
        ``Agent``'s system prompt. The user prompt carries the code under
        review plus an explicit schema hint. The words "accessibility",
        "wcag", and "issues" MUST appear here because
        ``DummyLLMClient.complete_json`` pattern-matches on them to return
        a deterministic stub in tests — see llm_service/README.md
        "Migration rule: keep pattern anchors in the user prompt".
        """
        parts = [
            "Review the following code for WCAG 2.2 accessibility issues. "
            "Produce structured JSON with fields: issues, summary, approved. "
            "Each issue must include severity, wcag_criterion, description, "
            "location, and recommendation.",
            "",
            f"**Language:** {input_data.language}",
        ]
        if input_data.task_description:
            parts.append(f"**Task:** {input_data.task_description}")
        parts.extend(
            [
                "**Code to review:**",
                "```",
                input_data.code,
                "```",
            ]
        )
        if input_data.architecture:
            parts.append(f"**Architecture:** {input_data.architecture.overview}")

        return "\n".join(parts)
