"""Deliver phase: package final handoff summary."""

from __future__ import annotations

import json

from strands import Agent

from llm_service import get_strands_model

from ..models import DeliverResult, ExecutionResult, ReviewResult
from ..prompts import DELIVER_PROMPT


def run_deliver(
    *, llm=None, execution_result: ExecutionResult, review_result: ReviewResult
) -> DeliverResult:
    prompt = (
        f"Execution summary: {execution_result.summary}\n"
        f"Generated files: {list(execution_result.files.keys())}\n"
        f"Review passed: {review_result.passed}\n"
        f"Review issues: {[issue.description for issue in review_result.issues]}\n"
    )
    from strands.models.model import Model as _StrandsModel

    _model = llm if (llm is not None and isinstance(llm, _StrandsModel)) else get_strands_model()
    agent = Agent(model=_model, system_prompt=DELIVER_PROMPT)
    result = agent(prompt)
    raw_text = str(result).strip()
    raw = json.loads(raw_text)
    return DeliverResult(
        summary=raw.get("summary", ""),
        handoff_notes=raw.get("handoff_notes") or [],
        runbook=raw.get("runbook") or [],
    )
