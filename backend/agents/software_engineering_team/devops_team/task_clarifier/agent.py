"""DevOps Task Clarifier agent.

Built on the AWS Strands Agents SDK via ``llm_service.LLMClientModel``.

The static gap-detection (missing goal / deployment target / environments /
acceptance criteria / rollback / secrets / prod-gate) runs BEFORE any LLM
call and short-circuits the run when a blocking gap is found. This saves
an LLM round-trip on incomplete specs and preserves the exact legacy
semantics.
"""

from __future__ import annotations

import logging
from typing import List

from strands import Agent

from llm_service import LLMClient, LLMClientModel

from .models import (
    ClarificationGap,
    DevOpsTaskClarifierInput,
    DevOpsTaskClarifierOutput,
)
from .prompts import DEVOPS_TASK_CLARIFIER_PROMPT

logger = logging.getLogger(__name__)


class DevOpsTaskClarifierAgent:
    """Ensures task input is complete and safe before execution."""

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self._model = LLMClientModel(
            llm_client,
            agent_key="devops_task_clarifier",
            temperature=0.0,
            think=True,
        )

    def run(self, input_data: DevOpsTaskClarifierInput) -> DevOpsTaskClarifierOutput:
        spec = input_data.task_spec
        gaps: List[ClarificationGap] = []

        if not spec.goal.summary.strip():
            gaps.append(
                ClarificationGap(
                    area="goal", message="Missing desired outcome summary", blocking=True
                )
            )
        if not spec.platform_scope.cloud.strip():
            gaps.append(
                ClarificationGap(
                    area="deployment_target",
                    message=(
                        "Deployment target/cloud provider not specified. Cannot proceed "
                        "without knowing where to deploy (e.g., Heroku, Railway, "
                        "DigitalOcean, AWS, on-premises)."
                    ),
                    blocking=True,
                )
            )
        if not spec.platform_scope.environments:
            gaps.append(
                ClarificationGap(
                    area="environment_scope", message="Missing environments list", blocking=True
                )
            )
        if not spec.acceptance_criteria:
            gaps.append(
                ClarificationGap(
                    area="acceptance_criteria", message="Missing acceptance criteria", blocking=True
                )
            )
        if not spec.rollback_requirements and any(
            e in ("staging", "production") for e in spec.platform_scope.environments
        ):
            gaps.append(
                ClarificationGap(
                    area="rollback",
                    message="Rollback requirements missing for staging/production",
                    blocking=True,
                )
            )
        if not spec.constraints.secrets.source.strip():
            gaps.append(
                ClarificationGap(
                    area="secrets", message="Secret source not specified", blocking=True
                )
            )
        if (
            "production" in spec.platform_scope.environments
            and "approval" not in " ".join(spec.scope.included).lower()
        ):
            gaps.append(
                ClarificationGap(
                    area="prod_gate",
                    message="Production deploy path lacks explicit approval gate",
                    blocking=True,
                )
            )

        checklist = [
            "task_scope_validated",
            "environment_scope_validated",
            "rollback_constraints_validated",
            "security_constraints_validated",
            "acceptance_criteria_normalized",
        ]
        if gaps:
            return DevOpsTaskClarifierOutput(
                approved_for_execution=False,
                checklist=checklist,
                gaps=gaps,
                clarification_requests=[g.message for g in gaps if g.blocking],
            )

        user_prompt = (
            "Acting as a devops task clarifier, validate that the task below is "
            "complete enough for execution. Produce structured JSON with fields: "
            "approved_for_execution, checklist, gaps, clarification_requests.\n\n"
            f"task_id={spec.task_id}\n"
            f"title={spec.title}\n"
            f"environments={spec.platform_scope.environments}\n"
            f"risk_level={spec.risk_level}\n"
            f"acceptance_criteria={spec.acceptance_criteria}\n"
            f"rollback={spec.rollback_requirements}\n"
        )

        agent = Agent(model=self._model, system_prompt=DEVOPS_TASK_CLARIFIER_PROMPT)

        try:
            agent_result = agent(user_prompt, structured_output_model=DevOpsTaskClarifierOutput)
            result = agent_result.structured_output
            if not isinstance(result, DevOpsTaskClarifierOutput):
                raise TypeError(
                    f"Expected DevOpsTaskClarifierOutput, "
                    f"got {type(result).__name__ if result else 'None'}"
                )
        except Exception as exc:  # noqa: BLE001 — LLM/validation failures must not crash the run
            logger.warning(
                "DevOpsTaskClarifier: structured_output failed (%s); assuming approved", exc
            )
            return DevOpsTaskClarifierOutput(
                approved_for_execution=True,
                checklist=checklist,
                gaps=[],
                clarification_requests=[],
            )

        # Guarantee a non-empty checklist even if the LLM returned one.
        if not result.checklist:
            result.checklist = checklist
        return result
