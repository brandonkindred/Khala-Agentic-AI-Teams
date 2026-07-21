"""Documentation and runbook agent."""

from __future__ import annotations

from typing import Any, Dict, Optional

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
    """Produce runbook docs and a completion package for a devops task.

    Invariants: instance state is limited to ``llm`` and ``_model`` from the
    base; ``run`` is stateless across calls.
    """

    PROMPT = DOC_RUNBOOK_PROMPT
    temperature: Optional[float] = None
    think: Optional[bool] = None

    def build_context(self, input_data: DocumentationRunbookInput) -> str:
        """Build the runbook prompt context from task metadata and gates.

        Preconditions: ``input_data`` is a valid ``DocumentationRunbookInput``.
        Postconditions: returns the same context string shape the pre-migration
        agent appended after the prompt separator.
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
        Postconditions: returns ``DocumentationRunbookOutput`` with the same
        non-LLM ``DevOpsCompletionPackage`` construction and the same
        ``files`` / ``summary`` defaults as the pre-migration agent.
        """
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
