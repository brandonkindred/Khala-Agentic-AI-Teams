"""
Build Specialist adapter stub for the backend-code-v2 team.

Triggered when the project doesn't build; can be wired to build_verifier or build-fix flow.
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


class BuildSpecialistAdapterAgent:
    """Stub Build Specialist tool agent — extend to assist when build fails."""

    def run(self, inp: ToolAgentInput) -> ToolAgentOutput:
        return self.execute(inp)

    def execute(self, inp: ToolAgentInput) -> ToolAgentOutput:
        logger.info("Build Specialist stub: microtask %s (not yet implemented)", inp.microtask.id)
        return ToolAgentOutput(
            summary="Build Specialist stub — no changes applied.",
            recommendations=["Integrate with build verifier or build-fix flow for full support."],
        )

    def plan(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(
            recommendations=["Ensure build configuration and dependencies are in scope."],
            summary="Build Specialist planning stub.",
        )

    def review(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(
            recommendations=["If project doesn't build, trigger Build Specialist to assist."],
            summary="Build Specialist review stub.",
        )

    def problem_solve(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(
            recommendations=["Diagnose build failures and suggest fixes (deps, config, compiler)."],
            summary="Build Specialist problem-solving stub.",
        )

    def deliver(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(
            summary="Build Specialist deliver stub — ensure build passes before merge.",
        )
