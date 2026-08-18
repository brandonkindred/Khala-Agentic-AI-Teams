"""Documentation and runbook agent."""

from __future__ import annotations

from typing import Any, Dict

from software_engineering_team.devops_team._agent_template import DevOpsSingleShotAgent
from software_engineering_team.devops_team.models import (
    DevOpsCompletionPackage,
    GitOperationsMetadata,
    HandoffInfo,
    ReleaseReadiness,
)

from .models import DocumentationRunbookInput, DocumentationRunbookOutput
from .prompts import DOC_RUNBOOK_PROMPT


class DocumentationRunbookAgent(DevOpsSingleShotAgent):
    """Produces runbook/documentation artifacts and the completion package.

    Invariants: instance state is limited to ``llm`` and ``_model`` from the
    base; ``run`` is stateless across calls.
    """

    PROMPT = DOC_RUNBOOK_PROMPT
    temperature = None
    think = None
    CACHE_NAMESPACE = "devops:doc_runbook:v1"
    CACHE_ENV_VAR = "DEVOPS_DOC_RUNBOOK_CACHE_SIZE"
    OUTPUT_MODEL = DocumentationRunbookOutput

    def build_context(self, input_data: DocumentationRunbookInput) -> str:
        """Build the runbook prompt context from the task and its artifacts.

        Preconditions: ``input_data`` is a valid ``DocumentationRunbookInput``.
        Postconditions: returns the same context string shape the
        pre-migration agent appended after the prompt separator.
        """
        return (
            f"task_id={input_data.task_id}\n"
            f"task_title={input_data.task_title}\n"
            f"artifacts={list(input_data.artifacts.keys())}\n"
            f"quality_gates={input_data.quality_gates}\n"
            f"notes={input_data.notes}\n"
        )

    def build_output(
        self, input_data: DocumentationRunbookInput, data: Dict[str, Any]
    ) -> DocumentationRunbookOutput:
        """Map the LLM JSON dict onto ``DocumentationRunbookOutput``.

        Preconditions: ``data`` is the dict from ``complete_json_with_continuation``.
        Postconditions: returns a ``DocumentationRunbookOutput`` whose
        ``files`` are the LLM-generated documentation content and whose
        ``completion_package`` is assembled from ``input_data`` (not the LLM
        reply) with a fixed rolling/rollback-available release posture.
        """
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
        return DocumentationRunbookOutput(
            files=data.get("files") or {},
            completion_package=completion,
            summary=data.get("summary", ""),
        )


def clear_review_cache() -> None:
    """Drop every cached documentation runbook result. Intended for test teardown."""
    DocumentationRunbookAgent.clear_cache()
