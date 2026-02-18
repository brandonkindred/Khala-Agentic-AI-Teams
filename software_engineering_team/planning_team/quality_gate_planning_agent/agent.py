"""Quality Gate Planning agent: assigns quality gates to task nodes."""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from shared.llm import LLMClient

from .models import QualityGatePlanningInput, QualityGatePlanningOutput
from .prompts import QUALITY_GATE_PLANNING_PROMPT

logger = logging.getLogger(__name__)

DEFAULT_BACKEND_GATES = ["code_review", "qa", "dbc"]
DEFAULT_FRONTEND_GATES = ["code_review", "qa", "accessibility", "dbc"]


class QualityGatePlanningAgent:
    """Assigns quality gates to task nodes."""

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def run(self, input_data: QualityGatePlanningInput) -> QualityGatePlanningOutput:
        """Assign quality gates to tasks."""
        if not input_data.task_ids:
            return QualityGatePlanningOutput(node_quality_gates={}, summary="")

        logger.info("Quality Gate Planning: starting for %s tasks", len(input_data.task_ids))
        context_parts = [
            "**Task IDs:**",
            ", ".join(input_data.task_ids[:30]),
        ]
        if input_data.delivery_strategy:
            context_parts.extend(["", "**Delivery strategy:**", input_data.delivery_strategy])

        prompt = QUALITY_GATE_PLANNING_PROMPT + "\n\n---\n\n" + "\n".join(context_parts)
        data = self.llm.complete_json(prompt, temperature=0.1)
        raw = data.get("node_quality_gates") or {}
        node_quality_gates = {}
        for tid in input_data.task_ids:
            gates = raw.get(tid)
            if isinstance(gates, list):
                node_quality_gates[tid] = [str(g) for g in gates]
            elif tid in input_data.task_ids:
                node_quality_gates[tid] = DEFAULT_BACKEND_GATES
        logger.info("Quality Gate Planning: assigned gates for %s tasks", len(node_quality_gates))
        return QualityGatePlanningOutput(
            node_quality_gates=node_quality_gates,
            summary=data.get("summary", ""),
        )
