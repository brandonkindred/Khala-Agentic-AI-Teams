"""Data Planning agent: adds data-related tasks when relevant."""

from __future__ import annotations

import logging
from typing import Any, Dict

from planning_team.planning_graph import (
    EdgeType,
    PlanningDomain,
    PlanningEdge,
    PlanningGraph,
    PlanningNode,
    PlanningNodeKind,
)
from shared.llm import LLMClient
from shared.models import ProductRequirements, SystemArchitecture

from .models import DataPlanningInput, DataPlanningOutput
from .prompts import DATA_PLANNING_PROMPT

logger = logging.getLogger(__name__)


def _parse_graph_from_llm_output(data: Dict[str, Any]) -> PlanningGraph:
    graph = PlanningGraph()
    for n in data.get("nodes") or []:
        if not isinstance(n, dict) or not n.get("id"):
            continue
        domain_str = (n.get("domain") or "data").lower()
        try:
            domain = PlanningDomain(domain_str)
        except ValueError:
            domain = PlanningDomain.DATA
        graph.add_node(PlanningNode(
            id=n["id"],
            domain=domain,
            kind=PlanningNodeKind.TASK,
            summary=n.get("summary", ""),
            details=n.get("details", ""),
            acceptance_criteria=n.get("acceptance_criteria", []),
        ))
    for e in data.get("edges") or []:
        if not isinstance(e, dict) or not e.get("from_id") or not e.get("to_id"):
            continue
        graph.add_edge(PlanningEdge(from_id=e["from_id"], to_id=e["to_id"], type=EdgeType.BLOCKS))
    return graph


class DataPlanningAgent:
    """Adds data-related tasks when the project involves significant data work."""

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def run(self, input_data: DataPlanningInput) -> DataPlanningOutput:
        """Generate data planning graph (may be empty)."""
        logger.info("Data Planning: starting for %s", input_data.requirements.title)
        context_parts = [
            f"**Product Title:** {input_data.requirements.title}",
            f"**Description:** {input_data.requirements.description[:2000]}",
        ]
        if input_data.architecture:
            context_parts.extend(["", "**Architecture:**", input_data.architecture.overview[:2000]])
        if input_data.spec_content:
            context_parts.extend(["", "**Spec (excerpt):**", (input_data.spec_content or "")[:4000]])

        prompt = DATA_PLANNING_PROMPT + "\n\n---\n\n" + "\n".join(context_parts)
        data = self.llm.complete_json(prompt, temperature=0.2)
        graph = _parse_graph_from_llm_output(data)
        logger.info("Data Planning: produced %s nodes", len(graph.nodes))
        return DataPlanningOutput(planning_graph=graph, summary=data.get("summary", ""))
