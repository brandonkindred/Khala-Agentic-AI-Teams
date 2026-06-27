"""
CI/CD adapter stub for the backend-code-v2 team.

This is a thin adapter that can be extended to call DevOps Team APIs.
No code from ``backend_agent`` is used.
"""

from __future__ import annotations

from ..static_agents import StubToolAgent


class CicdAdapterAgent(StubToolAgent):
    """Stub CI/CD tool agent — extend to delegate to the DevOps team."""

    label = "CI/CD"
    execute_summary = "CI/CD adapter stub — no changes applied."
    execute_recommendations = ["Integrate with DevOps Team CI/CD agents for full support."]
    plan_recommendations = ["Include CI/CD pipeline and deployment steps in the plan."]
    plan_summary = "CI/CD planning stub."
    review_recommendations = ["Validate pipeline config and build scripts."]
    review_summary = "CI/CD review stub."
    problem_solve_recommendations = ["Fix pipeline failures and dependency issues."]
    problem_solve_summary = "CI/CD problem-solving stub."
    deliver_summary = "CI/CD deliver stub — validate pipeline before merge."
