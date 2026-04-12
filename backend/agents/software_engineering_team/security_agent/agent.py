"""Cybersecurity Expert agent: security reviews and vulnerability remediation.

Built on the AWS Strands Agents SDK via ``llm_service.LLMClientModel``. The
``LLMClient`` passed in at construction time is wrapped into a Strands
``Model`` so the agent inherits retries, per-agent model routing, telemetry,
and the dummy-client path for tests.
"""

from __future__ import annotations

import logging

from strands import Agent

from llm_service import LLMClient, LLMClientModel

from .models import SecurityInput, SecurityOutput
from .prompts import SECURITY_PROMPT

logger = logging.getLogger(__name__)


class CybersecurityExpertAgent:
    """
    Cybersecurity expert that reviews code for security flaws and resolves
    any identified vulnerabilities.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self._model = LLMClientModel(
            llm_client,
            agent_key="security",
            temperature=0.1,
            think=True,
        )

    def run(self, input_data: SecurityInput) -> SecurityOutput:
        """Review code for security issues and produce fixed code."""
        logger.info("Security: reviewing %s chars of code", len(input_data.code or ""))

        user_prompt = self._build_user_prompt(input_data)

        # A fresh Strands Agent per call — reusing the same instance across
        # calls breaks structured_output forced-tool-choice on the second
        # call (Strands accumulates message history).
        agent = Agent(model=self._model, system_prompt=SECURITY_PROMPT)

        try:
            agent_result = agent(user_prompt, structured_output_model=SecurityOutput)
            result = agent_result.structured_output
            if not isinstance(result, SecurityOutput):
                raise TypeError(
                    f"Expected SecurityOutput, got {type(result).__name__ if result else 'None'}"
                )
        except Exception as exc:  # noqa: BLE001 — LLM/validation failures must not crash the run
            logger.warning("Security: structured_output failed (%s); returning fallback", exc)
            return SecurityOutput(
                vulnerabilities=[],
                approved=False,
                summary=f"Security analysis failed: {exc}",
                remediations=[],
                suggested_commit_message="",
            )

        # Re-derive ``approved`` from severities so a disagreement between the
        # LLM's ``approved`` flag and the reported vulnerability list is
        # resolved in favor of the vulnerability list.
        critical_or_high = [v for v in result.vulnerabilities if v.severity in ("critical", "high")]
        result.approved = len(critical_or_high) == 0

        logger.info(
            "Security: done, %s issues found, approved=%s",
            len(result.vulnerabilities),
            result.approved,
        )
        return result

    @staticmethod
    def _build_user_prompt(input_data: SecurityInput) -> str:
        """Assemble the user-facing prompt.

        The persona (``SECURITY_PROMPT``) lives on the Strands ``Agent``'s
        system prompt. The user prompt carries the code under review plus
        an explicit schema hint. The words "security" and "vulnerabilities"
        MUST appear here because ``DummyLLMClient.complete_json``
        pattern-matches on them to return a deterministic stub in tests —
        see llm_service/README.md "Migration rule: keep pattern anchors in
        the user prompt".
        """
        parts = [
            "Review the following code for security vulnerabilities. Produce "
            "structured JSON with fields: vulnerabilities, summary, "
            "remediations, suggested_commit_message. Each vulnerability must "
            "include severity, category, description, location, and "
            "recommendation.",
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
        if input_data.context:
            parts.append(f"**Context:** {input_data.context}")
        if input_data.architecture:
            parts.append(f"**Architecture:** {input_data.architecture.overview}")

        return "\n".join(parts)
