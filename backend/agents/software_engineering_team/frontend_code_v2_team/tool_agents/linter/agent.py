"""Linter tool agent stub for frontend-code-v2."""

from __future__ import annotations

import logging
from ...models import ToolAgentInput, ToolAgentOutput, ToolAgentPhaseInput, ToolAgentPhaseOutput

logger = logging.getLogger(__name__)


class LinterToolAgent:
    def run(self, inp: ToolAgentInput) -> ToolAgentOutput:
        return self.execute(inp)

    def execute(self, inp: ToolAgentInput) -> ToolAgentOutput:
        logger.info("Linter stub: microtask %s", inp.microtask.id)
        return ToolAgentOutput(summary="Linter stub — no changes applied.")

    def plan(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(recommendations=["Include lint rules and format in the plan."], summary="Linter planning stub.")

    def review(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(summary="Linter review stub.")

    def problem_solve(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(summary="Linter problem-solving stub.")

    def deliver(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(summary="Linter deliver stub.")
