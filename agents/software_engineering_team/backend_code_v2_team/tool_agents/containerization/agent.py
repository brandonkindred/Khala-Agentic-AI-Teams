"""
Containerization adapter stub for the backend-code-v2 team.

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


class ContainerizationAdapterAgent:
    """Stub containerization tool agent — extend to delegate to the DevOps team."""

    def run(self, inp: ToolAgentInput) -> ToolAgentOutput:
        return self.execute(inp)

    def execute(self, inp: ToolAgentInput) -> ToolAgentOutput:
        logger.info("Containerization stub: microtask %s (not yet implemented)", inp.microtask.id)
        return ToolAgentOutput(
            summary="Containerization adapter stub — no changes applied.",
            recommendations=["Integrate with DevOps Team deployment agents for full support."],
        )

    def plan(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(
            recommendations=["Include Dockerfile and container config in the plan."],
            summary="Containerization planning stub.",
        )

    def review(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(
            recommendations=["Verify image build and runtime config."],
            summary="Containerization review stub.",
        )

    def problem_solve(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(
            recommendations=["Fix image layers and dependency installation."],
            summary="Containerization problem-solving stub.",
        )

    def deliver(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(
            summary="Containerization deliver stub — validate image before merge.",
        )
