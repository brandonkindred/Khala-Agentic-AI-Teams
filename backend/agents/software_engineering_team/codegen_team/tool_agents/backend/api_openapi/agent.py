"""
API / OpenAPI tool agent: contract design, endpoint implementation, spec validation.

Implemented from scratch inside the backend-code-v2 team.
Uses template-based output (not JSON) so parsing works across model providers.
"""

from __future__ import annotations

from strands import Agent  # noqa: F401  (kept so tests can monkeypatch this module's Agent)

from software_engineering_team.codegen_team.stacks.backend.profile import (
    parse_files_and_summary_template,
)
from software_engineering_team.codegen_team.stacks.backend.prompts import (
    FILES_OUTPUT_TEMPLATE_INSTRUCTIONS,
)

from ..static_agents import FileGeneratorToolAgent

API_OPENAPI_PROMPT = (
    """You are an API / OpenAPI specialist.

Given a microtask about REST endpoint design, OpenAPI specification, or
service contract work, produce the required files (routers, schemas,
openapi.yaml fragments, etc.).

**Microtask:** {description}
**Language:** {language}
**Existing code context:** {existing_code}
"""
    + FILES_OUTPUT_TEMPLATE_INSTRUCTIONS
)


class ApiOpenApiToolAgent(FileGeneratorToolAgent):
    """Produces API routes, OpenAPI specs, and service contracts."""

    log_label = "ApiOpenApi"
    generation_prompt = API_OPENAPI_PROMPT
    _parse_files_and_summary = staticmethod(parse_files_and_summary_template)

    plan_recommendations = ["Include API contract and OpenAPI spec in the microtask plan."]
    plan_summary = "API/OpenAPI planning input provided."
    review_recommendations = ["Verify OpenAPI spec matches implemented endpoints."]
    review_summary = "API/OpenAPI review completed."
    problem_solve_recommendations = [
        "Align contract and implementation; fix status codes and schemas."
    ]
    problem_solve_summary = "API/OpenAPI problem-solving input provided."
    deliver_summary = "API/OpenAPI deliver phase completed."
