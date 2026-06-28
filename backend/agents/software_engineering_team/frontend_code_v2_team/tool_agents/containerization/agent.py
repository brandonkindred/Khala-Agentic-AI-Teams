"""Containerization adapter stub for frontend-code-v2.

Declarative :class:`StubToolAgent` profile — frontend containerization is not yet
implemented, so every phase returns a static advisory message via the shared
static base.
"""

from __future__ import annotations

from software_engineering_team.shared.tool_agent_static import StubToolAgent


class ContainerizationAdapterAgent(StubToolAgent):
    """Frontend containerization adapter stub (no changes applied)."""

    label = "Containerization"
    execute_summary = "Containerization stub — no changes applied."
    plan_recommendations = ["Consider Dockerfile for frontend app."]
    plan_summary = "Containerization planning stub."
    review_summary = "Containerization review stub."
    problem_solve_summary = "Containerization problem-solving stub."
    deliver_summary = "Containerization deliver stub."
