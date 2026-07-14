"""Deliver phase: package final handoff summary."""

from __future__ import annotations

from software_engineering_team.shared.llm import complete_json_with_continuation

from ..models import DeliverResult, ExecutionResult, ReviewResult
from ..prompts import DELIVER_PROMPT


def run_deliver(
    *, llm=None, execution_result: ExecutionResult, review_result: ReviewResult
) -> DeliverResult:
    """Package the execution and review outcome into a final handoff summary.

    Preconditions:
        ``execution_result`` and ``review_result`` are valid results produced
        by the prior phases. ``llm`` is a Strands ``Model``, an ``LLMClient``,
        or ``None``.
    Postconditions:
        Returns a ``DeliverResult`` built from the parsed JSON response, with
        missing fields defaulting to an empty string or empty list.
    """
    prompt = (
        f"Execution summary: {execution_result.summary}\n"
        f"Generated files: {list(execution_result.files.keys())}\n"
        f"Review passed: {review_result.passed}\n"
        f"Review issues: {[issue.description for issue in review_result.issues]}\n"
    )
    raw = complete_json_with_continuation(llm, prompt, system_prompt=DELIVER_PROMPT)
    return DeliverResult(
        summary=raw.get("summary", ""),
        handoff_notes=raw.get("handoff_notes") or [],
        runbook=raw.get("runbook") or [],
    )
