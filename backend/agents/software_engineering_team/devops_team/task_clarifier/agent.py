"""DevOps Task Clarifier agent."""

from __future__ import annotations

from typing import List

from llm_service import LLMClient, get_strands_model
from llm_service.strands_model import model_fingerprint, resolve_strands_model
from software_engineering_team.devops_team._llm_cache import (
    build_cache_key,
    cache_capacity,
    clear_cache,
    get_cached_result,
    set_cached_result,
)
from software_engineering_team.shared.llm import complete_json_with_continuation

from .models import (
    ClarificationGap,
    DevOpsTaskClarifierInput,
    DevOpsTaskClarifierOutput,
)
from .prompts import DEVOPS_TASK_CLARIFIER_PROMPT

# Shared review-result cache: keyed on the whole DevOpsTaskClarifierInput
# content plus the resolved model. Only covers the LLM-backed tail of run()
# — the deterministic gaps check above it always runs and never touches the
# cache. See ``devops_team._llm_cache`` for the shared implementation.
_CACHE_NAMESPACE = "devops:task_clarifier:v1"
_CACHE_ENV_VAR = "DEVOPS_TASK_CLARIFIER_CACHE_SIZE"
_CACHE_DEFAULT_SIZE = 128


def clear_task_clarifier_cache() -> None:
    """Drop every cached task clarifier result. Intended for test teardown."""
    clear_cache(_CACHE_NAMESPACE, log_prefix="DevOpsTaskClarifierAgent")


class DevOpsTaskClarifierAgent:
    """Ensures task input is complete and safe before execution."""

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client
        self._model = resolve_strands_model(
            llm_client, agent_key="devops", get_strands_model_fn=get_strands_model
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

        capacity = cache_capacity(_CACHE_ENV_VAR, _CACHE_DEFAULT_SIZE)
        cache_key = None
        if capacity > 0:
            cache_key = build_cache_key(input_data, model_fingerprint(self._model))
            cached = get_cached_result(
                _CACHE_NAMESPACE,
                cache_key,
                DevOpsTaskClarifierOutput,
                log_prefix="DevOpsTaskClarifierAgent",
            )
            if cached is not None:
                return cached

        context = (
            f"task_id={spec.task_id}\n"
            f"title={spec.title}\n"
            f"environments={spec.platform_scope.environments}\n"
            f"risk_level={spec.risk_level}\n"
            f"acceptance_criteria={spec.acceptance_criteria}\n"
            f"rollback={spec.rollback_requirements}\n"
        )
        data = complete_json_with_continuation(
            self._model,
            DEVOPS_TASK_CLARIFIER_PROMPT + "\n\n---\n\n" + context,
            temperature=0.0,
            think=True,
        )
        result = DevOpsTaskClarifierOutput(
            approved_for_execution=bool(data.get("approved_for_execution", True)),
            checklist=data.get("checklist") or checklist,
            gaps=[ClarificationGap(**g) for g in (data.get("gaps") or []) if isinstance(g, dict)],
            clarification_requests=data.get("clarification_requests") or [],
        )

        if cache_key is not None:
            set_cached_result(
                _CACHE_NAMESPACE, cache_key, result, capacity, log_prefix="DevOpsTaskClarifierAgent"
            )

        return result
