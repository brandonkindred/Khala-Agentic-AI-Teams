"""CI/CD tool agent for backend-code-v2: generates CI workflows from deterministic templates."""

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
    """CI/CD tool agent backed by deterministic Jinja2 templates.

    Invariants:
        ``deliver()`` always produces valid GitHub Actions YAML via
        ``ci_templates.render_backend_ci`` — no LLM involved.
    """

    def run(self, inp: ToolAgentInput) -> ToolAgentOutput:
        return self.execute(inp)

    def execute(self, inp: ToolAgentInput) -> ToolAgentOutput:
        logger.info("CI/CD: microtask %s (execute — no changes applied)", inp.microtask.id)
        return ToolAgentOutput(summary="CI/CD execute — no changes applied.")

    def plan(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(
            recommendations=[
                "CI pipeline will include: ruff lint, pytest, bandit (SAST), pip-audit (SCA), gitleaks (secrets).",
                "Workflows are generated from deterministic templates, not LLM.",
            ],
            summary="CI/CD planning: template-based pipeline.",
        )

    def review(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(
            summary="CI/CD review: template-generated pipeline is deterministic."
        )

    def problem_solve(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(
            recommendations=["Fix pipeline failures and dependency issues."],
            summary="CI/CD problem-solving.",
        )

    def deliver(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Generate backend CI workflow from template.

        Postconditions:
            ``files`` contains a valid ``.github/workflows/backend.yml`` entry.
        """
        try:
            from software_engineering_team.ci_templates import BackendCIParams, render_backend_ci

            workflow = render_backend_ci(BackendCIParams())
            return ToolAgentPhaseOutput(
                files={".github/workflows/backend.yml": workflow},
                summary="Generated backend CI workflow from template.",
            )
        except Exception as e:
            logger.warning("CI/CD deliver failed: %s", e)
            return ToolAgentPhaseOutput(summary=f"CI/CD deliver failed: {e}")
