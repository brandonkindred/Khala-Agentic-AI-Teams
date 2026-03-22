"""Documentation and runbook agent."""

from __future__ import annotations

from devops_team.models import (
    DevOpsCompletionPackage,
    GitOperationsMetadata,
    HandoffInfo,
    ReleaseReadiness,
)

from llm_service import LLMClient

from .models import DocumentationRunbookInput, DocumentationRunbookOutput
from .prompts import DOC_RUNBOOK_PROMPT


class DocumentationRunbookAgent:
    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def run(self, input_data: DocumentationRunbookInput) -> DocumentationRunbookOutput:
        context = (
            f"task_id={input_data.task_id}\n"
            f"task_title={input_data.task_title}\n"
            f"artifacts={list(input_data.artifacts.keys())}\n"
            f"quality_gates={input_data.quality_gates}\n"
            f"notes={input_data.notes}\n"
        )
        data = self.llm.complete_json(DOC_RUNBOOK_PROMPT + "\n\n---\n\n" + context, temperature=0.1)
        completion = DevOpsCompletionPackage(
            task_id=input_data.task_id,
            status="completed",
            files_changed=sorted(input_data.artifacts.keys()),
            quality_gates={k: v for k, v in input_data.quality_gates.items()},
            release_readiness=ReleaseReadiness(
                deployment_strategy="rolling",
                rollback_available=True,
                alerting_configured=True,
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
