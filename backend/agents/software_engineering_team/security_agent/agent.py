"""Cybersecurity Expert agent: security reviews and vulnerability remediation.

Built on the AWS Strands Agents SDK via ``llm_service.get_strands_model``. The
model returned by ``get_strands_model`` is passed to a Strands ``Agent`` so the
agent inherits retries, per-agent model routing, telemetry, and the
dummy-client path for tests.
"""

from __future__ import annotations

import logging

from strands import Agent

from llm_service import get_strands_model
from software_engineering_team.shared.security_service import derive_approved

from .models import SecurityInput, SecurityOutput
from .prompts import SECURITY_PROMPT

logger = logging.getLogger(__name__)


class CybersecurityExpertAgent:
    """
    Cybersecurity expert that reviews code for security flaws and resolves
    any identified vulnerabilities.
    """

    def __init__(self, llm_client=None) -> None:
        """Resolve the review model.

        Preconditions: ``llm_client`` is ``None``, an ``LLMClient``, or a Strands
        ``Model``.
        Postconditions: ``self._model`` is a usable Strands model — the passed
        client when it is already a Strands ``Model``, else the ``security`` model.
        """
        from strands.models.model import Model as _StrandsModel

        if llm_client is not None and isinstance(llm_client, _StrandsModel):
            self._model = llm_client
        else:
            self._model = get_strands_model("security")

    def run(self, input_data: SecurityInput) -> SecurityOutput:
        """Review code for security vulnerabilities.

        Preconditions:
            ``input_data`` is a ``SecurityInput`` (``code`` may be empty); the
            configured model supports forced structured output.
        Postconditions:
            Returns a ``SecurityOutput`` whose ``approved`` is re-derived by the
            unified rule (:func:`derive_approved`): ``approved`` is ``False`` iff
            any vulnerability is critical/high severity (severity comparison is
            case-insensitive), regardless of any ``approved`` flag the model
            returned. On any model/validation failure, returns a safe fallback
            with ``approved=False`` and no vulnerabilities.
        """
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

        # Re-derive ``approved`` via the unified rule so a disagreement between
        # the LLM's ``approved`` flag and the reported vulnerability list is
        # resolved in favor of the vulnerability list. ``SecurityVulnerability``
        # has no ``blocking`` attribute, so this reduces to "no critical/high".
        result.approved = derive_approved(result.vulnerabilities, llm_approved=None)

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
