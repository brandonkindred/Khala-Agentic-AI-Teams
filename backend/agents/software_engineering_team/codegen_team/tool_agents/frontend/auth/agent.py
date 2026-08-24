"""
Authentication and Authorization tool agent for frontend-code-v2: login UI,
route guards, token handling, protected routes.

Real implementation (mirrors the backend stack's ``AuthToolAgent`` shape).
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

AUTH_PROMPT = (
    """You are an expert Frontend Authentication specialist.

Given a microtask about login/logout UI, route guards, token storage and
refresh, or protected-route handling, produce the required files (auth
service/store, route guards or interceptors, login/logout components, etc.)
for the detected framework (Angular/React/Vue).

**Microtask:** {description}
**Language/stack:** {language}
**Existing code context:** {existing_code}
"""
    + FILES_OUTPUT_TEMPLATE_INSTRUCTIONS
)


class AuthToolAgent(FileGeneratorToolAgent):
    """Produces frontend authentication UI, route guards, and token handling."""

    log_label = "Auth"
    generation_prompt = AUTH_PROMPT
    _parse_files_and_summary = staticmethod(parse_files_and_summary_template)

    plan_recommendations = ["Include route guards and token storage/refresh in the microtask plan."]
    plan_summary = "Auth planning input provided."
    review_recommendations = [
        "Check for tokens stored insecurely (e.g. localStorage without justification) "
        "and missing route guards on protected routes."
    ]
    review_summary = "Auth review completed."
    problem_solve_recommendations = ["Fix token handling and route guard checks as needed."]
    problem_solve_summary = "Auth problem-solving input provided."
    deliver_summary = "Auth deliver phase completed."
