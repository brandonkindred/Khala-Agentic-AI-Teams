"""
API / OpenAPI tool agent for frontend-code-v2: typed API client generation,
service layer, request/response DTOs.

Real implementation (mirrors the backend stack's ``ApiOpenApiToolAgent`` shape).
Uses template-based output (not JSON) so parsing works across model providers.
"""

from __future__ import annotations

from strands import Agent  # noqa: F401  (kept so tests can monkeypatch this module's Agent)

from software_engineering_team.codegen_team.stacks.frontend.profile import (
    parse_files_and_summary_template,
)
from software_engineering_team.codegen_team.stacks.frontend.prompts import (
    FILES_OUTPUT_TEMPLATE_INSTRUCTIONS,
)
from software_engineering_team.shared.tool_agent_static import FileGeneratorToolAgent

API_OPENAPI_PROMPT = (
    """You are an expert Frontend API/OpenAPI specialist.

Given a microtask about consuming a REST/OpenAPI backend from the frontend,
produce the required files: a typed API client/service layer
(HttpClient-based service for Angular, a fetch/axios-based service or hook
for React/Vue), request/response type definitions, and error-handling
wrappers.

**Microtask:** {description}
**Language/stack:** {language}
**Existing code context:** {existing_code}
"""
    + FILES_OUTPUT_TEMPLATE_INSTRUCTIONS
)


class ApiOpenApiToolAgent(FileGeneratorToolAgent):
    """Produces typed API client/service code consuming a REST/OpenAPI backend."""

    log_label = "ApiOpenApi"
    generation_prompt = API_OPENAPI_PROMPT
    _parse_files_and_summary = staticmethod(parse_files_and_summary_template)

    plan_recommendations = ["Include the API client/service layer in the microtask plan."]
    plan_summary = "API/OpenAPI planning input provided."
    review_recommendations = [
        "Verify request/response types match the backend contract and errors are handled."
    ]
    review_summary = "API/OpenAPI review completed."
    problem_solve_recommendations = [
        "Align client types and error handling with the backend contract."
    ]
    problem_solve_summary = "API/OpenAPI problem-solving input provided."
    deliver_summary = "API/OpenAPI deliver phase completed."
