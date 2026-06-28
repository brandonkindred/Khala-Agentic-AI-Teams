"""Auth tool agent stub for frontend-code-v2.

Declarative :class:`StubToolAgent` profile — frontend auth is not yet implemented,
so every phase returns a static advisory message via the shared static base.
"""

from __future__ import annotations

from software_engineering_team.shared.tool_agent_static import StubToolAgent


class AuthToolAgent(StubToolAgent):
    """Frontend auth adapter stub (no changes applied)."""

    label = "Auth"
    execute_summary = "Auth stub — no changes applied."
    plan_recommendations = ["Consider login UI and auth guards."]
    plan_summary = "Auth planning stub."
    review_summary = "Auth review stub."
    problem_solve_summary = "Auth problem-solving stub."
    deliver_summary = "Auth deliver stub."
