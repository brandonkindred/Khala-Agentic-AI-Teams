"""
CI/CD adapter stub for the backend-code-v2 team.

This is a thin adapter that can be extended to call DevOps Team APIs.
No code from ``backend_agent`` is used.
"""

from __future__ import annotations

import logging

from ...models import (
    ToolAgentInput,
    ToolAgentOutput,
    ToolAgentPhaseInput,
    ToolAgentPhaseOutput,
)

logger = logging.getLogger(__name__)


class CicdAdapterAgent:
    """Stub CI/CD tool agent — extend to delegate to the DevOps team."""

    def run(self, inp: ToolAgentInput) -> ToolAgentOutput:
        return self.execute(inp)

    def execute(self, inp: ToolAgentInput) -> ToolAgentOutput:
        logger.info("CI/CD stub: microtask %s (not yet implemented)", inp.microtask.id)
        return ToolAgentOutput(
            summary="CI/CD adapter stub — no changes applied.",
            recommendations=["Integrate with DevOps Team CI/CD agents for full support."],
        )

    def plan(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(
            recommendations=["Include CI/CD pipeline and deployment steps in the plan."],
            summary="CI/CD planning stub.",
        )

    def review(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(
            recommendations=["Validate pipeline config and build scripts."],
            summary="CI/CD review stub.",
        )

    def problem_solve(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(
            recommendations=["Fix pipeline failures and dependency issues."],
            summary="CI/CD problem-solving stub.",
        )

    def deliver(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(
            summary="CI/CD deliver stub — validate pipeline before merge.",
        )
