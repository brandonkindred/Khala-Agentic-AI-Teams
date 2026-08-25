"""
Authentication and Authorization tool agent: login, RBAC, permissions.

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

AUTH_PROMPT = (
    """You are an expert Authentication and Authorization specialist.

Given a microtask about login, JWT, RBAC, permission gates, or secure defaults,
produce the required files (auth modules, middleware, permission models, etc.).

**Microtask:** {description}
**Language:** {language}
**Existing code context:** {existing_code}
"""
    + FILES_OUTPUT_TEMPLATE_INSTRUCTIONS
)


class AuthToolAgent(FileGeneratorToolAgent):
    """Produces authentication and authorization code and configurations."""

    log_label = "Auth"
    generation_prompt = AUTH_PROMPT
    _parse_files_and_summary = staticmethod(parse_files_and_summary_template)

    plan_recommendations = ["Include auth middleware and permission checks in the microtask plan."]
    plan_summary = "Auth planning input provided."
    review_recommendations = ["Check for hardcoded secrets and correct permission boundaries."]
    review_summary = "Auth review completed."
    problem_solve_recommendations = ["Fix token handling and permission checks as needed."]
    problem_solve_summary = "Auth problem-solving input provided."
    deliver_summary = "Auth deliver phase completed."
