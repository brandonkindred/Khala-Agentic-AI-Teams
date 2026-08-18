"""DevOps Task Clarifier agent."""

from __future__ import annotations

from typing import List

from llm_service import LLMClient, get_strands_model
from llm_service.strands_model import model_fingerprint, resolve_strands_model
from shared.cache import get_shared_cache
from software_engineering_team.shared.llm import complete_json_with_continuation
from software_engineering_team.shared.review_result_cache import (
    build_review_cache_key,
    cache_capacity_for,
    cache_namespace_for,
    clear_review_cache_namespace,
    get_cached_review_result,
    set_cached_review_result,
)

from .models import (
    ClarificationGap,
    DevOpsTaskClarifierInput,
    DevOpsTaskClarifierOutput,
)
from .prompts import DEVOPS_TASK_CLARIFIER_PROMPT

_CACHE_LABEL = "DevOpsTaskClarifier"

# Shared review-result cache: keyed on the whole DevOpsTaskClarifierInput
# content plus the resolved model. Only covers the LLM-backed tail of run()
# — the deterministic gaps check above it always runs and never touches the
# cache. The shared policy lives in
# ``software_engineering_team.shared.review_result_cache``; this module
# supplies only its own namespace stem, env var, capacity default, and
# output model.
_REVIEW_CACHE_NAMESPACE = "devops:task_clarifier:v1"
DEFAULT_REVIEW_CACHE_SIZE = 128  # DEVOPS_TASK_CLARIFIER_CACHE_SIZE, floor 0


def _review_cache_namespace() -> str:
    """Shared-cache namespace for task clarifier results (includes build id)."""
    return cache_namespace_for(_REVIEW_CACHE_NAMESPACE)


def _review_cache_size() -> int:
    """Resolve the review cache capacity from the environment."""
    return cache_capacity_for("DEVOPS_TASK_CLARIFIER_CACHE_SIZE", DEFAULT_REVIEW_CACHE_SIZE)


def clear_review_cache() -> None:
    """Drop every cached task clarifier result. Intended for test teardown."""
    clear_review_cache_namespace(_CACHE_LABEL, lambda: get_shared_cache(_review_cache_namespace()))


class DevOpsTaskClarifierAgent:
    """Ensures task input is complete and safe before execution.

    Invariants: instance state is limited to ``llm`` and ``_model``; ``run``
    is stateless across calls.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        """Store the review client and resolve its Strands model.

        Preconditions: ``llm_client`` is not ``None`` (an ``LLMClient``).
        Postconditions: ``self.llm`` is the stored client; ``self._model`` is
        the resolved Strands model under ``agent_key="devops"``.
        """
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client
        self._model = resolve_strands_model(
            llm_client, agent_key="devops", get_strands_model_fn=get_strands_model
        )

    def run(self, input_data: DevOpsTaskClarifierInput) -> DevOpsTaskClarifierOutput:
        """Validate the task spec is complete and, if so, ask the LLM to review it.

        Preconditions:
            ``input_data`` is a valid ``DevOpsTaskClarifierInput``.
        Postconditions:
            When any deterministic gap is found (missing goal/cloud/
            environments/acceptance-criteria/secrets-source, a
            staging-or-production environment with no rollback plan, or a
            production environment with no approval gate in scope), returns
            ``approved_for_execution=False`` immediately with those gaps —
            no LLM call, no cache lookup. Otherwise, a cache hit
            (byte-identical ``input_data`` and resolved model) returns the
            prior result without invoking the LLM. A cache miss, a disabled
            cache (``DEVOPS_TASK_CLARIFIER_CACHE_SIZE=0``), or any
            cache-backend error falls open to a genuine call, and every
            result reaching the cache-write step is written back (this
            method has no fallback branch of its own past the gaps check).
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

        capacity = _review_cache_size()
        cache_key = None
        if capacity > 0:
            cache_key = build_review_cache_key(input_data, model_fingerprint(self._model))
            cache = get_shared_cache(_review_cache_namespace())
            cached = get_cached_review_result(
                _CACHE_LABEL, cache, cache_key, DevOpsTaskClarifierOutput
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
            cache = get_shared_cache(_review_cache_namespace())
            set_cached_review_result(_CACHE_LABEL, cache, cache_key, result, capacity=capacity)

        return result
