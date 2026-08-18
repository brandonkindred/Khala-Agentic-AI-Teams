"""Documentation and runbook agent."""

from __future__ import annotations

from llm_service import LLMClient, get_strands_model
from llm_service.strands_model import resolve_strands_model
from software_engineering_team.devops_team.models import (
    DevOpsCompletionPackage,
    GitOperationsMetadata,
    HandoffInfo,
    ReleaseReadiness,
)
from software_engineering_team.shared.llm import complete_json_with_continuation

from .models import DocumentationRunbookInput, DocumentationRunbookOutput
from .prompts import DOC_RUNBOOK_PROMPT


class DocumentationRunbookAgent:
    """Produce documentation/runbook artifacts and the devops completion package.

    Invariants: instance state is limited to ``llm`` and the resolved Strands
    ``_model``; ``run`` is stateless across calls.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        """Resolve the devops-routed Strands model.

        Preconditions: ``llm_client`` is not ``None`` (an ``LLMClient`` or a
        Strands ``Model``).
        Postconditions: ``self.llm`` is the passed client; ``self._model`` is
        the resolved Strands model under ``agent_key="devops"``.
        """
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client
        self._model = resolve_strands_model(
            llm_client, agent_key="devops", get_strands_model_fn=get_strands_model
        )

    def run(self, input_data: DocumentationRunbookInput) -> DocumentationRunbookOutput:
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
        return DocumentationRunbookOutput(
            files=data.get("files") or {},
            completion_package=completion,
            summary=data.get("summary", ""),
        )
