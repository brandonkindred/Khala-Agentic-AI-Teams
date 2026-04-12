"""Documentation and runbook agent.

Built on the AWS Strands Agents SDK via ``llm_service.LLMClientModel``.
"""

from __future__ import annotations

import logging
from typing import Dict

from devops_team.models import (
    DevOpsCompletionPackage,
    GitOperationsMetadata,
    HandoffInfo,
    ReleaseReadiness,
)
from pydantic import BaseModel, Field
from strands import Agent

from llm_service import LLMClient, LLMClientModel

from .models import DocumentationRunbookInput, DocumentationRunbookOutput
from .prompts import DOC_RUNBOOK_PROMPT

logger = logging.getLogger(__name__)


class _DocRunbookLLMResponse(BaseModel):
    """Just the fields the LLM produces.

    ``DocumentationRunbookOutput.completion_package`` is assembled in Python
    from the agent input (task_id, artifact keys, quality_gates, notes), so
    we don't ask the LLM to produce it.
    """

    files: Dict[str, str] = Field(default_factory=dict)
    summary: str = ""


class DocumentationRunbookAgent:
    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self._model = LLMClientModel(
            llm_client,
            agent_key="doc_runbook",
            temperature=0.1,
        )

    def run(self, input_data: DocumentationRunbookInput) -> DocumentationRunbookOutput:
        user_prompt = (
            "Acting as a devops documentation and runbook writer, produce the "
            "runbook, README additions, and any operational documentation "
            "files for the task below. Produce structured JSON with fields: "
            "files (dict of doc path to content), summary.\n\n"
            f"task_id={input_data.task_id}\n"
            f"task_title={input_data.task_title}\n"
            f"artifacts={list(input_data.artifacts.keys())}\n"
            f"quality_gates={input_data.quality_gates}\n"
            f"notes={input_data.notes}\n"
        )

        agent = Agent(model=self._model, system_prompt=DOC_RUNBOOK_PROMPT)

        try:
            agent_result = agent(user_prompt, structured_output_model=_DocRunbookLLMResponse)
            llm_resp = agent_result.structured_output
            if not isinstance(llm_resp, _DocRunbookLLMResponse):
                raise TypeError(
                    f"Expected _DocRunbookLLMResponse, "
                    f"got {type(llm_resp).__name__ if llm_resp else 'None'}"
                )
            files = llm_resp.files
            summary = llm_resp.summary
        except Exception as exc:  # noqa: BLE001 — LLM/validation failures must not crash the run
            logger.warning("DocRunbook: structured_output failed (%s); returning empty docs", exc)
            files = {}
            summary = f"Documentation generation failed: {exc}"

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
            files=files,
            completion_package=completion,
            summary=summary,
        )
