"""API/OpenAPI tool agent stub for frontend-code-v2.

Declarative :class:`StubToolAgent` profile — frontend API/OpenAPI work is not yet
implemented, so every phase returns a static advisory message via the shared
static base.
"""

from __future__ import annotations

from software_engineering_team.shared.tool_agent_static import StubToolAgent


class ApiOpenApiToolAgent(StubToolAgent):
    """Frontend API/OpenAPI adapter stub (no changes applied)."""

    label = "API/OpenAPI"
    execute_summary = "API/OpenAPI stub — no changes applied."
    plan_recommendations = ["Consider API client and service layer."]
    plan_summary = "API planning stub."
    review_summary = "API review stub."
    problem_solve_summary = "API problem-solving stub."
    deliver_summary = "API deliver stub."
