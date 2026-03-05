"""Auth tool agent stub for frontend-code-v2."""

from __future__ import annotations

import logging
from ...models import ToolAgentInput, ToolAgentOutput, ToolAgentPhaseInput, ToolAgentPhaseOutput

logger = logging.getLogger(__name__)


class AuthToolAgent:
    def run(self, inp: ToolAgentInput) -> ToolAgentOutput:
        return self.execute(inp)

    def execute(self, inp: ToolAgentInput) -> ToolAgentOutput:
        logger.info("Auth stub: microtask %s", inp.microtask.id)
        return ToolAgentOutput(summary="Auth stub — no changes applied.")

    def plan(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(recommendations=["Consider login UI and auth guards."], summary="Auth planning stub.")

    def review(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(summary="Auth review stub.")

    def problem_solve(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(summary="Auth problem-solving stub.")

    def deliver(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(summary="Auth deliver stub.")
