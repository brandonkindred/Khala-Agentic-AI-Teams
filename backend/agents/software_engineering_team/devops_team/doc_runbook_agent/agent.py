"""Documentation and runbook agent."""

from __future__ import annotations

from llm_service import LLMClient, get_strands_model
from llm_service.strands_model import model_fingerprint, resolve_strands_model
from software_engineering_team.devops_team._llm_cache import (
    build_cache_key,
    cache_capacity,
    clear_cache,
    get_cached_result,
    set_cached_result,
)
from software_engineering_team.devops_team.models import (
    DevOpsCompletionPackage,
    GitOperationsMetadata,
    HandoffInfo,
    ReleaseReadiness,
)
from software_engineering_team.shared.llm import complete_json_with_continuation

from .models import DocumentationRunbookInput, DocumentationRunbookOutput
from .prompts import DOC_RUNBOOK_PROMPT

# Shared review-result cache: keyed on the whole DocumentationRunbookInput
# content plus the resolved model. See ``devops_team._llm_cache`` for the
# shared implementation.
_CACHE_NAMESPACE = "devops:doc_runbook:v1"
_CACHE_ENV_VAR = "DEVOPS_DOC_RUNBOOK_CACHE_SIZE"
_CACHE_DEFAULT_SIZE = 128


def clear_doc_runbook_cache() -> None:
    """Drop every cached documentation runbook result. Intended for test teardown."""
    clear_cache(_CACHE_NAMESPACE, log_prefix="DocumentationRunbookAgent")


class DocumentationRunbookAgent:
    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client
        self._model = resolve_strands_model(
            llm_client, agent_key="devops", get_strands_model_fn=get_strands_model
        )

    def run(self, input_data: DocumentationRunbookInput) -> DocumentationRunbookOutput:
        capacity = cache_capacity(_CACHE_ENV_VAR, _CACHE_DEFAULT_SIZE)
        cache_key = None
        if capacity > 0:
            cache_key = build_cache_key(input_data, model_fingerprint(self._model))
            cached = get_cached_result(
                _CACHE_NAMESPACE,
                cache_key,
                DocumentationRunbookOutput,
                log_prefix="DocumentationRunbookAgent",
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
            set_cached_result(
                _CACHE_NAMESPACE,
                cache_key,
                result,
                capacity,
                log_prefix="DocumentationRunbookAgent",
            )

        return result
