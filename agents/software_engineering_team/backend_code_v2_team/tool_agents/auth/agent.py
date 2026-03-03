"""
Authentication and Authorization tool agent: login, RBAC, permissions.

Implemented from scratch inside the backend-code-v2 team.
Uses template-based output (not JSON) so parsing works across model providers.
"""

from __future__ import annotations

import logging

from software_engineering_team.shared.llm import LLMClient

from ...models import (
    ToolAgentInput,
    ToolAgentOutput,
    ToolAgentPhaseInput,
    ToolAgentPhaseOutput,
)
from ...output_templates import parse_files_and_summary_template
from ...prompts import FILES_OUTPUT_TEMPLATE_INSTRUCTIONS

logger = logging.getLogger(__name__)

AUTH_PROMPT = """You are an Authentication and Authorization specialist.

Given a microtask about login, JWT, RBAC, permission gates, or secure defaults,
produce the required files (auth modules, middleware, permission models, etc.).

**Microtask:** {description}
**Language:** {language}
**Existing code context:** {existing_code}
""" + FILES_OUTPUT_TEMPLATE_INSTRUCTIONS


class AuthToolAgent:
    """Produces authentication and authorization code and configurations."""

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def run(self, inp: ToolAgentInput) -> ToolAgentOutput:
        return self.execute(inp)

    def execute(self, inp: ToolAgentInput) -> ToolAgentOutput:
        prompt = AUTH_PROMPT.format(
            description=inp.microtask.description or inp.microtask.title,
            language=inp.language,
            existing_code=inp.existing_code[:4000] if inp.existing_code else "(none)",
        )
        logger.info("Auth: running for microtask %s", inp.microtask.id)
        raw = self.llm.complete_text(prompt)
        data = parse_files_and_summary_template(raw)
        return ToolAgentOutput(
            files=data.get("files") or {},
            recommendations=[],
            summary=data.get("summary", ""),
        )

    def plan(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Recommend how auth/RBAC work should be reflected in the plan."""
        return ToolAgentPhaseOutput(
            recommendations=["Include auth middleware and permission checks in the microtask plan."],
            summary="Auth planning input provided.",
        )

    def review(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Domain-specific review: no hardcoded secrets, RBAC consistency."""
        return ToolAgentPhaseOutput(
            recommendations=["Check for hardcoded secrets and correct permission boundaries."],
            summary="Auth review completed.",
        )

    def problem_solve(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Suggest auth-layer fixes for issues found in review."""
        return ToolAgentPhaseOutput(
            recommendations=["Fix token handling and permission checks as needed."],
            summary="Auth problem-solving input provided.",
        )

    def deliver(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Final auth-domain actions before merge."""
        return ToolAgentPhaseOutput(summary="Auth deliver phase completed.")
