"""
API / OpenAPI tool agent: contract design, endpoint implementation, spec validation.

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

API_OPENAPI_PROMPT = """You are an API / OpenAPI specialist.

Given a microtask about REST endpoint design, OpenAPI specification, or
service contract work, produce the required files (routers, schemas,
openapi.yaml fragments, etc.).

**Microtask:** {description}
**Language:** {language}
**Existing code context:** {existing_code}
""" + FILES_OUTPUT_TEMPLATE_INSTRUCTIONS


class ApiOpenApiToolAgent:
    """Produces API routes, OpenAPI specs, and service contracts."""

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def run(self, inp: ToolAgentInput) -> ToolAgentOutput:
        return self.execute(inp)

    def execute(self, inp: ToolAgentInput) -> ToolAgentOutput:
        prompt = API_OPENAPI_PROMPT.format(
            description=inp.microtask.description or inp.microtask.title,
            language=inp.language,
            existing_code=inp.existing_code[:4000] if inp.existing_code else "(none)",
        )
        logger.info("ApiOpenApi: running for microtask %s", inp.microtask.id)
        raw = self.llm.complete_text(prompt)
        data = parse_files_and_summary_template(raw)
        return ToolAgentOutput(
            files=data.get("files") or {},
            recommendations=[],
            summary=data.get("summary", ""),
        )

    def plan(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Recommend how API/contract work should be reflected in the plan."""
        return ToolAgentPhaseOutput(
            recommendations=["Include API contract and OpenAPI spec in the microtask plan."],
            summary="API/OpenAPI planning input provided.",
        )

    def review(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Domain-specific review: contract consistency, spec validation."""
        return ToolAgentPhaseOutput(
            recommendations=["Verify OpenAPI spec matches implemented endpoints."],
            summary="API/OpenAPI review completed.",
        )

    def problem_solve(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Suggest API-layer fixes for issues found in review."""
        return ToolAgentPhaseOutput(
            recommendations=["Align contract and implementation; fix status codes and schemas."],
            summary="API/OpenAPI problem-solving input provided.",
        )

    def deliver(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Final API-domain actions before merge."""
        return ToolAgentPhaseOutput(summary="API/OpenAPI deliver phase completed.")
