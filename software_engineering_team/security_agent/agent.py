"""Cybersecurity Expert agent: security reviews and vulnerability remediation."""

from __future__ import annotations

import logging
from typing import Any, Dict

from shared.llm import LLMClient

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
        context_parts = [
            f"**Language:** {input_data.language}",
            f"**Code to review:**",
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
        data = self.llm.complete_json(prompt, temperature=0.1)

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

        fixed_code = data.get("fixed_code", input_data.code)
        if fixed_code and "\\n" in fixed_code:
            fixed_code = fixed_code.replace("\\n", "\n")

        return SecurityOutput(
            vulnerabilities=vulnerabilities,
            fixed_code=fixed_code,
            summary=data.get("summary", ""),
            remediations=data.get("remediations", []),
        )
