"""Cybersecurity Expert agent: security review and vulnerability reporting.

Calls the LLM via ``shared.single_shot_review.run_single_shot_review`` in
schema-validated mode, which resolves the client, validates the reply
against ``SecurityLLMResponse``, and drives one bounded corrective retry
(re-prompting with the schema/validation error) before falling back — in
place of the single-shot, no-retry Strands ``structured_output_model`` path
this agent used previously.
"""

from __future__ import annotations

import logging

from llm_service import LLMClient
from software_engineering_team.shared.security_service import derive_approved
from software_engineering_team.shared.single_shot_review import run_single_shot_review

from .models import SecurityInput, SecurityLLMResponse, SecurityOutput
from .prompts import SECURITY_PROMPT

logger = logging.getLogger(__name__)


class CybersecurityExpertAgent:
    """
    Cybersecurity expert that reviews code for security flaws. Reports
    vulnerabilities for the coding agent to remediate — never patches them
    itself (see ``SecurityOutput.vulnerabilities``).
    """

    def __init__(self, llm_client: LLMClient | None = None) -> None:
        """Store the injectable review client.

        Preconditions: ``llm_client`` is ``None`` or an ``LLMClient``.
        Postconditions: ``self.llm`` is ``llm_client`` unchanged —
        ``run_single_shot_review`` resolves the default ``security`` client
        per call when it is ``None``.
        """
        self.llm = llm_client

    def run(self, input_data: SecurityInput) -> SecurityOutput:
        """Review code for security vulnerabilities.

        Preconditions:
            ``input_data`` is a ``SecurityInput`` (``code`` may be empty).
        Postconditions:
            Returns a ``SecurityOutput`` whose ``approved`` is re-derived by the
            unified rule (:func:`derive_approved`): ``approved`` is ``False`` iff
            any vulnerability is critical/high severity (severity comparison is
            case-insensitive), regardless of any ``approved`` flag the model
            returned. On any model/validation failure surviving
            ``run_single_shot_review``'s corrective retry, returns a safe
            fallback with ``approved=False`` and no vulnerabilities. Never
            raises.
        """
        logger.info("Security: reviewing %s chars of code", len(input_data.code or ""))

        user_prompt = self._build_user_prompt(input_data)

        try:
            response = run_single_shot_review(
                self.llm,
                agent_key="security",
                prompt=user_prompt,
                system_prompt=SECURITY_PROMPT,
                schema=SecurityLLMResponse,
                objective="security review",
                temperature=0.0,
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
        # the LLM's ``approved`` flag (not part of ``SecurityLLMResponse`` — the
        # prompt never asks for one) and the reported vulnerability list is
        # resolved in favor of the vulnerability list. ``SecurityVulnerability``
        # has no ``blocking`` attribute, so this reduces to "no critical/high".
        result = SecurityOutput(
            vulnerabilities=response.vulnerabilities,
            summary=response.summary,
            remediations=response.remediations,
        )
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

        The persona (``SECURITY_PROMPT``) is passed as
        ``run_single_shot_review``'s ``system_prompt``. The user prompt
        carries the code under review plus an explicit schema hint. The
        words "security" and "vulnerabilities" MUST appear here because
        ``DummyLLMClient.complete_json`` pattern-matches on them to return a
        deterministic stub in tests — see llm_service/README.md "Migration
        rule: keep pattern anchors in the user prompt".
        """
        parts = [
            "Review the following code for security vulnerabilities. Produce "
            "structured JSON with fields: vulnerabilities, summary, "
            "remediations. Each vulnerability must include severity, "
            "category, description, location, and recommendation.",
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
