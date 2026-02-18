"""Performance Planning agent: adds performance budgets and optional perf tasks."""

from __future__ import annotations

import logging
from typing import Any, Dict

from planning_team.planning_graph import (
    PlanningDomain,
    PlanningEdge,
    PlanningGraph,
    PlanningNode,
    PlanningNodeKind,
)
from shared.llm import LLMClient
from shared.models import ProductRequirements, SystemArchitecture

from .models import PerformancePlanningInput, PerformancePlanningOutput
from .prompts import PERFORMANCE_PLANNING_PROMPT

logger = logging.getLogger(__name__)


class PerformancePlanningAgent:
    """Adds performance budgets to nodes and optional performance tasks."""

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def run(self, input_data: PerformancePlanningInput) -> PerformancePlanningOutput:
        """Generate performance planning augmentations."""
        if not input_data.existing_node_ids:
            return PerformancePlanningOutput(planning_graph=PlanningGraph(), node_budgets={}, summary="")

        logger.info("Performance Planning: starting for %s", input_data.requirements.title)
        context_parts = [
            f"**Product Title:** {input_data.requirements.title}",
            "**Existing task IDs:**",
            ", ".join(input_data.existing_node_ids[:20]),
        ]
        if input_data.architecture:
            context_parts.extend(["", "**Architecture:**", input_data.architecture.overview[:1500]])
        if input_data.spec_content:
            context_parts.extend(["", "**Spec (excerpt):**", (input_data.spec_content or "")[:3000]])

        prompt = PERFORMANCE_PLANNING_PROMPT + "\n\n---\n\n" + "\n".join(context_parts)
        data = self.llm.complete_json(prompt, temperature=0.1)
        node_budgets = {k: str(v) for k, v in (data.get("node_budgets") or {}).items() if k in input_data.existing_node_ids}
        graph = PlanningGraph()
        for n in data.get("nodes") or []:
            if not isinstance(n, dict) or not n.get("id"):
                continue
            graph.add_node(PlanningNode(
                id=n["id"],
                domain=PlanningDomain.BACKEND,
                kind=PlanningNodeKind.TASK,
                summary=n.get("summary", ""),
                details=n.get("details", ""),
                acceptance_criteria=n.get("acceptance_criteria", []),
                performance_budget=n.get("performance_budget"),
            ))
        logger.info("Performance Planning: %s budgets, %s new nodes", len(node_budgets), len(graph.nodes))
        return PerformancePlanningOutput(planning_graph=graph, node_budgets=node_budgets, summary=data.get("summary", ""))
