"""Documentation Planning agent: adds doc tasks with DOCUMENTS edges."""

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

from .models import DocumentationPlanningInput, DocumentationPlanningOutput
from .prompts import DOCUMENTATION_PLANNING_PROMPT

logger = logging.getLogger(__name__)


def _parse_graph_from_llm_output(data: Dict[str, Any]) -> PlanningGraph:
    graph = PlanningGraph()
    for n in data.get("nodes") or []:
        if not isinstance(n, dict) or not n.get("id"):
            continue
        graph.add_node(PlanningNode(
            id=n["id"],
            domain=PlanningDomain.DOCS,
            kind=PlanningNodeKind.TASK,
            summary=n.get("summary", ""),
            details=n.get("details", ""),
            acceptance_criteria=n.get("acceptance_criteria", []),
            metadata=n.get("metadata", {}),
        ))
    for e in data.get("edges") or []:
        if not isinstance(e, dict) or not e.get("from_id") or not e.get("to_id"):
            continue
        type_str = (e.get("type") or "documents").lower()
        try:
            edge_type = EdgeType(type_str)
        except ValueError:
            edge_type = EdgeType.DOCUMENTS
        graph.add_edge(PlanningEdge(from_id=e["from_id"], to_id=e["to_id"], type=edge_type))
    return graph


class DocumentationPlanningAgent:
    """Adds documentation tasks."""

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def run(self, input_data: DocumentationPlanningInput) -> DocumentationPlanningOutput:
        """Generate documentation planning graph."""
        logger.info("Documentation Planning: starting for %s", input_data.requirements.title)
        context_parts = [
            f"**Product Title:** {input_data.requirements.title}",
            "**Existing task IDs:**",
            ", ".join(input_data.existing_task_ids[:15]) if input_data.existing_task_ids else "None",
        ]
        if input_data.architecture:
            context_parts.extend(["", "**Architecture:**", input_data.architecture.overview[:1500]])

        prompt = DOCUMENTATION_PLANNING_PROMPT + "\n\n---\n\n" + "\n".join(context_parts)
        data = self.llm.complete_json(prompt, temperature=0.2)
        graph = _parse_graph_from_llm_output(data)
        logger.info("Documentation Planning: produced %s nodes", len(graph.nodes))
        return DocumentationPlanningOutput(planning_graph=graph, summary=data.get("summary", ""))
