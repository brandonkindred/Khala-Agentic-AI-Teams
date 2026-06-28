"""
Containerization adapter stub for the backend-code-v2 team.

No code from ``backend_agent`` is used.
"""

from __future__ import annotations

from ..static_agents import StubToolAgent


class ContainerizationAdapterAgent(StubToolAgent):
    """Stub containerization tool agent — extend to delegate to the DevOps team."""

    label = "Containerization"
    execute_summary = "Containerization adapter stub — no changes applied."
    execute_recommendations = ["Integrate with DevOps Team deployment agents for full support."]
    plan_recommendations = ["Include Dockerfile and container config in the plan."]
    plan_summary = "Containerization planning stub."
    review_recommendations = ["Verify image build and runtime config."]
    review_summary = "Containerization review stub."
    problem_solve_recommendations = ["Fix image layers and dependency installation."]
    problem_solve_summary = "Containerization problem-solving stub."
    deliver_summary = "Containerization deliver stub — validate image before merge."
