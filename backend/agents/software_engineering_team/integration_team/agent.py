"""Integration / API-contract agent: validates full-stack backend-frontend alignment.

Built on the AWS Strands Agents SDK via ``llm_service.get_strands_model``: the
model returned by ``get_strands_model`` is passed to a Strands ``Agent`` so the
agent inherits retries, per-agent model routing, telemetry, and the
dummy-client path for tests.
"""

from __future__ import annotations

import logging

from strands import Agent

from llm_service import get_strands_model

from .models import IntegrationInput, IntegrationOutput
from .prompts import INTEGRATION_PROMPT

logger = logging.getLogger(__name__)


class IntegrationAgent:
    """
    Integration expert that validates backend API and frontend are correctly aligned.
    Detects contract mismatches, missing endpoints, wrong payload shapes.
    """

    def __init__(self, llm_client=None) -> None:
        if llm_client is not None:
            self._model = llm_client
        else:
            self._model = get_strands_model("integration")

    def run(self, input_data: IntegrationInput) -> IntegrationOutput:
        """Analyze backend and frontend code for integration/contract issues."""
        logger.info(
            "Integration: analyzing backend (%s chars) and frontend (%s chars)",
            len(input_data.backend_code or ""),
            len(input_data.frontend_code or ""),
        )

        user_prompt = self._build_user_prompt(input_data)

        # A fresh Strands Agent per call. Strands' Agent accumulates message
        # history across invocations; reusing the same instance breaks the
        # forced-tool-choice mechanism used by ``structured_output_model``
        # on the second call. Construction is cheap — it just wraps the
        # cached model reference with a system_prompt.
        agent = Agent(model=self._model, system_prompt=INTEGRATION_PROMPT)

        try:
            agent_result = agent(user_prompt, structured_output_model=IntegrationOutput)
            result = agent_result.structured_output
            if not isinstance(result, IntegrationOutput):
                raise TypeError(
                    f"Expected IntegrationOutput, got {type(result).__name__ if result else 'None'}"
                )
        except Exception as exc:  # noqa: BLE001 — agent failures should not crash the run
            logger.warning(
                "Integration: structured_output failed (%s); returning failed result", exc
            )
            return IntegrationOutput(
                passed=False,
                issues=[],
                summary=f"Integration analysis failed: {exc}",
                fix_task_suggestions=[],
            )

        # Trust the returned issues but re-derive ``passed`` from severities so a
        # disagreement between the LLM's ``passed`` flag and the reported issues
        # is resolved in favor of the issue list.
        critical_or_high = [i for i in result.issues if i.severity in ("critical", "high")]
        result.passed = len(critical_or_high) == 0

        logger.info(
            "Integration: done, %s issues (%s critical/high), passed=%s",
            len(result.issues),
            len(critical_or_high),
            result.passed,
        )
        return result

    @staticmethod
    def _build_user_prompt(input_data: IntegrationInput) -> str:
        """Assemble the user-facing prompt.

        Note: the system prompt (``INTEGRATION_PROMPT``) is handed to the
        Strands ``Agent`` separately. We still include the phrases
        "integration expert", "backend code", and "frontend code" in the
        user-visible prompt because ``DummyLLMClient`` pattern-matches on
        those to return a deterministic stub in tests.
        """
        parts = [
            "You are acting as an integration expert. Analyze the following backend "
            "code and frontend code for contract mismatches and report findings as "
            "structured JSON.",
            "",
            "**Backend code:**",
            "```",
            input_data.backend_code or "# No backend code",
            "```",
            "",
            "**Frontend code:**",
            "```",
            input_data.frontend_code or "# No frontend code",
            "```",
        ]
        if input_data.spec_content:
            parts.extend(
                [
                    "",
                    "**Project specification:**",
                    "---",
                    input_data.spec_content[:8000],
                    "---",
                ]
            )
        if input_data.architecture:
            parts.append(f"**Architecture:** {input_data.architecture.overview}")

        return "\n".join(parts)
