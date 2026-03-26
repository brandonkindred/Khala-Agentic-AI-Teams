"""Cybersecurity Expert agent: security reviews and vulnerability remediation."""

from __future__ import annotations

import logging

from llm_service import LLMClient

from .models import SecurityInput, SecurityOutput, SecurityVulnerability
from .prompts import SECURITY_PROMPT

logger = logging.getLogger(__name__)


class CybersecurityExpertAgent:
    """
    Cybersecurity expert that reviews code for security flaws and resolves
    any identified vulnerabilities.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def run(self, input_data: SecurityInput) -> SecurityOutput:
        """Review code for security issues and produce fixed code."""
        logger.info("Security: reviewing %s chars of code", len(input_data.code or ""))
        context_parts = [
            f"**Language:** {input_data.language}",
            "**Code to review:**",
            "```",
            input_data.code,
            "```",
        ]
        if input_data.task_description:
            context_parts.insert(2, f"**Task:** {input_data.task_description}")
        if input_data.context:
            context_parts.append(f"**Context:** {input_data.context}")
        if input_data.architecture:
            context_parts.append(f"**Architecture:** {input_data.architecture.overview}")

        prompt = SECURITY_PROMPT + "\n\n---\n\n" + "\n".join(context_parts)
        data = self.llm.complete_json(prompt, temperature=0.1, think=True)

        vulnerabilities = []
        for v in data.get("vulnerabilities") or []:
            if isinstance(v, dict) and v.get("description"):
                vulnerabilities.append(
                    SecurityVulnerability(
                        severity=v.get("severity", "medium"),
                        category=v.get("category", "general"),
                        description=v["description"],
                        location=v.get("location", ""),
                        recommendation=v.get("recommendation", ""),
                    )
                )

        critical_vulns = [v for v in vulnerabilities if v.severity in ("critical", "high")]
        approved = len(critical_vulns) == 0

        logger.info("Security: done, %s issues found, approved=%s", len(vulnerabilities), approved)
        return SecurityOutput(
            vulnerabilities=vulnerabilities,
            approved=approved,
            summary=data.get("summary", ""),
            remediations=data.get("remediations", []),
            suggested_commit_message=data.get("suggested_commit_message", ""),
        )
