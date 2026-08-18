"""Documentation and runbook agent."""

from __future__ import annotations

from llm_service import LLMClient, get_strands_model
from llm_service.strands_model import model_fingerprint, resolve_strands_model
from shared.cache import get_shared_cache
from software_engineering_team.devops_team.models import (
    DevOpsCompletionPackage,
    GitOperationsMetadata,
    HandoffInfo,
    ReleaseReadiness,
)
from software_engineering_team.shared.llm import complete_json_with_continuation
from software_engineering_team.shared.review_result_cache import (
    build_review_cache_key,
    cache_capacity_for,
    cache_namespace_for,
    clear_review_cache_namespace,
    get_cached_review_result,
    set_cached_review_result,
)

from .models import DocumentationRunbookInput, DocumentationRunbookOutput
from .prompts import DOC_RUNBOOK_PROMPT

_CACHE_LABEL = "DocumentationRunbook"

# Shared review-result cache: keyed on the whole DocumentationRunbookInput
# content plus the resolved model. The shared policy lives in
# ``software_engineering_team.shared.review_result_cache``; this module
# supplies only its own namespace stem, env var, capacity default, and
# output model.
_REVIEW_CACHE_NAMESPACE = "devops:doc_runbook:v1"
DEFAULT_REVIEW_CACHE_SIZE = 128  # DEVOPS_DOC_RUNBOOK_CACHE_SIZE, floor 0


def _review_cache_namespace() -> str:
    """Shared-cache namespace for documentation runbook results (includes build id)."""
    return cache_namespace_for(_REVIEW_CACHE_NAMESPACE)


def _review_cache_size() -> int:
    """Resolve the review cache capacity from the environment."""
    return cache_capacity_for("DEVOPS_DOC_RUNBOOK_CACHE_SIZE", DEFAULT_REVIEW_CACHE_SIZE)


def clear_review_cache() -> None:
    """Drop every cached documentation runbook result. Intended for test teardown."""
    clear_review_cache_namespace(_CACHE_LABEL, lambda: get_shared_cache(_review_cache_namespace()))


class DocumentationRunbookAgent:
    """Produces runbook/documentation artifacts and the completion package.

    Invariants: instance state is limited to the injectable ``llm`` client
    and the resolved Strands ``_model``; ``run`` is stateless across calls.
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

    def run(self, input_data: DocumentationRunbookInput) -> DocumentationRunbookOutput:
        """Generate runbook/documentation files and the completion package.

        Preconditions:
            ``input_data`` is a valid ``DocumentationRunbookInput``.
        Postconditions:
            Returns a ``DocumentationRunbookOutput`` whose ``files`` are the
            LLM-generated documentation content and whose
            ``completion_package`` is assembled from ``input_data`` (not the
            LLM reply) with a fixed rolling/rollback-available release
            posture. A cache hit (byte-identical ``input_data`` and resolved
            model) returns the prior result without invoking the LLM. A
            cache miss, a disabled cache (``DEVOPS_DOC_RUNBOOK_CACHE_SIZE=0``),
            or any cache-backend error falls open to a genuine call. Every
            result reaching the cache-write step is written back — this
            method has no fallback branch of its own (unlike
            ``DevSecOpsReviewAgent.run``), so there is no non-genuine result
            to exclude.
        """
        capacity = _review_cache_size()
        cache_key = None
        if capacity > 0:
            cache_key = build_review_cache_key(input_data, model_fingerprint(self._model))
            cache = get_shared_cache(_review_cache_namespace())
            cached = get_cached_review_result(
                _CACHE_LABEL, cache, cache_key, DocumentationRunbookOutput
            )
            if cached is not None:
                return cached

        context = (
            f"task_id={input_data.task_id}\n"
            f"task_title={input_data.task_title}\n"
            f"artifacts={list(input_data.artifacts.keys())}\n"
            f"quality_gates={input_data.quality_gates}\n"
            f"notes={input_data.notes}\n"
        )
        data = complete_json_with_continuation(
            self._model, DOC_RUNBOOK_PROMPT + "\n\n---\n\n" + context
        )
        completion = DevOpsCompletionPackage(
            task_id=input_data.task_id,
            status="completed",
            files_changed=sorted(input_data.artifacts.keys()),
            quality_gates={k: v for k, v in input_data.quality_gates.items()},
            release_readiness=ReleaseReadiness(
                deployment_strategy="rolling",
                rollback_available=True,
                alerting_configured=False,
            ),
            notes=input_data.notes,
            git_operations=GitOperationsMetadata(),
            handoff=HandoffInfo(prod_approval_required=True, runbook_updated=True),
        )
        result = DocumentationRunbookOutput(
            files=data.get("files") or {},
            completion_package=completion,
            summary=data.get("summary", ""),
        )

        if cache_key is not None:
            cache = get_shared_cache(_review_cache_namespace())
            set_cached_review_result(_CACHE_LABEL, cache, cache_key, result, capacity=capacity)

        return result
