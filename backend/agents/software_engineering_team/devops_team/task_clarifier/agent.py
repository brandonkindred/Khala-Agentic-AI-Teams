"""DevOps Task Clarifier agent."""

from __future__ import annotations

from typing import List

from software_engineering_team.shared.llm import LLMClient

from .models import (
    ClarificationGap,
    DevOpsTaskClarifierInput,
    DevOpsTaskClarifierOutput,
)
from .prompts import DEVOPS_TASK_CLARIFIER_PROMPT


class DevOpsTaskClarifierAgent:
    """Ensures task input is complete and safe before execution."""

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def run(self, input_data: DevOpsTaskClarifierInput) -> DevOpsTaskClarifierOutput:
        spec = input_data.task_spec
        gaps: List[ClarificationGap] = []

        if not spec.goal.summary.strip():
            gaps.append(ClarificationGap(area="goal", message="Missing desired outcome summary", blocking=True))
        if not spec.platform_scope.cloud.strip():
            gaps.append(ClarificationGap(
                area="deployment_target",
                message="Deployment target/cloud provider not specified. Cannot proceed without knowing where to deploy (e.g., Heroku, Railway, DigitalOcean, AWS, on-premises).",
                blocking=True
            ))
        if not spec.platform_scope.environments:
            gaps.append(ClarificationGap(area="environment_scope", message="Missing environments list", blocking=True))
        if not spec.acceptance_criteria:
            gaps.append(ClarificationGap(area="acceptance_criteria", message="Missing acceptance criteria", blocking=True))
        if not spec.rollback_requirements and any(e in ("staging", "production") for e in spec.platform_scope.environments):
            gaps.append(ClarificationGap(area="rollback", message="Rollback requirements missing for staging/production", blocking=True))
        if not spec.constraints.secrets.source.strip():
            gaps.append(ClarificationGap(area="secrets", message="Secret source not specified", blocking=True))
        if "production" in spec.platform_scope.environments and "approval" not in " ".join(spec.scope.included).lower():
            gaps.append(ClarificationGap(area="prod_gate", message="Production deploy path lacks explicit approval gate", blocking=True))

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

        context = (
            f"task_id={spec.task_id}\n"
            f"title={spec.title}\n"
            f"environments={spec.platform_scope.environments}\n"
            f"risk_level={spec.risk_level}\n"
            f"acceptance_criteria={spec.acceptance_criteria}\n"
            f"rollback={spec.rollback_requirements}\n"
        )
        data = self.llm.complete_json(DEVOPS_TASK_CLARIFIER_PROMPT + "\n\n---\n\n" + context, temperature=0.0)
        return DevOpsTaskClarifierOutput(
            approved_for_execution=bool(data.get("approved_for_execution", True)),
            checklist=data.get("checklist") or checklist,
            gaps=[ClarificationGap(**g) for g in (data.get("gaps") or []) if isinstance(g, dict)],
            clarification_requests=data.get("clarification_requests") or [],
        )
