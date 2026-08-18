"""DevOps Task Clarifier agent."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from software_engineering_team.devops_team._agent_template import DevOpsSingleShotAgent

from .models import (
    ClarificationGap,
    DevOpsTaskClarifierInput,
    DevOpsTaskClarifierOutput,
)
from .prompts import DEVOPS_TASK_CLARIFIER_PROMPT

_CHECKLIST = [
    "task_scope_validated",
    "environment_scope_validated",
    "rollback_constraints_validated",
    "security_constraints_validated",
    "acceptance_criteria_normalized",
]


class DevOpsTaskClarifierAgent(DevOpsSingleShotAgent):
    """Ensures task input is complete and safe before execution.

    Invariants: instance state is limited to ``llm`` and ``_model`` from the
    base. ``run`` is deterministic for identical inputs and the resolved
    model: repeated identical calls may return a cached result and skip the
    LLM (unless a deterministic gap short-circuits first). Cache reads/writes
    are fail-open and gated by ``CACHE_ENV_VAR``.
    """

    PROMPT = DEVOPS_TASK_CLARIFIER_PROMPT
    temperature = 0.0
    CACHE_NAMESPACE = "devops:task_clarifier:v1"
    CACHE_ENV_VAR = "DEVOPS_TASK_CLARIFIER_CACHE_SIZE"
    OUTPUT_MODEL = DevOpsTaskClarifierOutput

    def pre_call(self, input_data: DevOpsTaskClarifierInput) -> Optional[DevOpsTaskClarifierOutput]:
        """Run the deterministic completeness/safety checks on the task spec.

        Preconditions: ``input_data`` is a valid ``DevOpsTaskClarifierInput``.
        Postconditions: returns ``None`` when no deterministic gap is found,
        letting the caller proceed to the LLM/cached-LLM path. Returns a
        ``DevOpsTaskClarifierOutput`` with ``approved_for_execution=False``
        immediately when any gap is found (missing goal/cloud/environments/
        acceptance-criteria/secrets-source, a staging-or-production
        environment with no rollback plan, or a production environment with
        no approval gate in scope) — no LLM call, no cache lookup; that
        output's ``checklist`` is a copy of the module-level ``_CHECKLIST``,
        ``gaps`` is the collected list of ``ClarificationGap`` objects, and
        ``clarification_requests`` is the ``message`` of each blocking gap.
        """
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
                    message="Deployment target/cloud provider not specified. Cannot proceed without knowing where to deploy (e.g., Heroku, Railway, DigitalOcean, AWS, on-premises).",
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

        if gaps:
            return DevOpsTaskClarifierOutput(
                approved_for_execution=False,
                checklist=list(_CHECKLIST),
                gaps=gaps,
                clarification_requests=[g.message for g in gaps if g.blocking],
            )
        return None

    def build_context(self, input_data: DevOpsTaskClarifierInput) -> str:
        """Build the clarifier prompt context from the task spec.

        Preconditions: ``input_data`` is a valid ``DevOpsTaskClarifierInput``
        that passed ``pre_call`` (no deterministic gaps).
        Postconditions: returns the same context string shape the
        pre-migration agent appended after the prompt separator.
        """
        spec = input_data.task_spec
        return (
            f"task_id={spec.task_id}\n"
            f"title={spec.title}\n"
            f"environments={spec.platform_scope.environments}\n"
            f"risk_level={spec.risk_level}\n"
            f"acceptance_criteria={spec.acceptance_criteria}\n"
            f"rollback={spec.rollback_requirements}\n"
        )

    def build_output(
        self, input_data: DevOpsTaskClarifierInput, data: Dict[str, Any]
    ) -> DevOpsTaskClarifierOutput:
        """Map the LLM JSON dict onto ``DevOpsTaskClarifierOutput``.

        Preconditions: ``data`` is the dict from ``complete_json_with_continuation``.
        Postconditions: returns a ``DevOpsTaskClarifierOutput`` whose
        ``checklist`` falls back to the module ``_CHECKLIST`` when the LLM
        omits one.
        """
        return DevOpsTaskClarifierOutput(
            approved_for_execution=bool(data.get("approved_for_execution", True)),
            checklist=data.get("checklist") or list(_CHECKLIST),
            gaps=[ClarificationGap(**g) for g in (data.get("gaps") or []) if isinstance(g, dict)],
            clarification_requests=data.get("clarification_requests") or [],
        )


def clear_review_cache() -> None:
    """Drop every cached task clarifier result. Intended for test teardown."""
    DevOpsTaskClarifierAgent.clear_cache()
