"""API/OpenAPI tool agent stub for frontend-code-v2."""

from __future__ import annotations

import logging
from ...models import ToolAgentInput, ToolAgentOutput, ToolAgentPhaseInput, ToolAgentPhaseOutput

logger = logging.getLogger(__name__)


class ApiOpenApiToolAgent:
    def run(self, inp: ToolAgentInput) -> ToolAgentOutput:
        return self.execute(inp)

    def execute(self, inp: ToolAgentInput) -> ToolAgentOutput:
        logger.info("API/OpenAPI stub: microtask %s", inp.microtask.id)
        return ToolAgentOutput(summary="API/OpenAPI stub — no changes applied.")

    def plan(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(recommendations=["Consider API client and service layer."], summary="API planning stub.")

    def review(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(summary="API review stub.")

    def problem_solve(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(summary="API problem-solving stub.")

    def deliver(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(summary="API deliver stub.")
