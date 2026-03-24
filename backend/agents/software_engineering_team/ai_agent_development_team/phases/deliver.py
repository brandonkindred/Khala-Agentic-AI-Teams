"""Deliver phase: package final handoff summary."""

from __future__ import annotations

from llm_service import LLMClient

from ..models import DeliverResult, ExecutionResult, ReviewResult
from ..prompts import DELIVER_PROMPT


def run_deliver(
    *, llm: LLMClient, execution_result: ExecutionResult, review_result: ReviewResult
) -> DeliverResult:
    prompt = (
        f"{DELIVER_PROMPT}\n\n"
        f"Execution summary: {execution_result.summary}\n"
        f"Generated files: {list(execution_result.files.keys())}\n"
        f"Review passed: {review_result.passed}\n"
        f"Review issues: {[issue.description for issue in review_result.issues]}\n"
    )
    raw = llm.complete_json(prompt)
    return DeliverResult(
        summary=raw.get("summary", ""),
        handoff_notes=raw.get("handoff_notes") or [],
        runbook=raw.get("runbook") or [],
    )
