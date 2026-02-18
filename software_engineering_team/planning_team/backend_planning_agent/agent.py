"""Backend Planning agent: produces backend-specific PlanningGraph slice."""

from __future__ import annotations

import logging
from typing import Any, Dict, List

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

from .models import BackendPlanningInput, BackendPlanningOutput
from .prompts import BACKEND_PLANNING_PROMPT

logger = logging.getLogger(__name__)


def _parse_graph_from_llm_output(data: Dict[str, Any]) -> PlanningGraph:
    """Parse LLM JSON output into PlanningGraph."""
    graph = PlanningGraph()
    for n in data.get("nodes") or []:
        if not isinstance(n, dict) or not n.get("id"):
            continue
        kind_str = (n.get("kind") or "task").lower()
        try:
            kind = PlanningNodeKind(kind_str)
        except ValueError:
            kind = PlanningNodeKind.TASK
        meta = dict(n.get("metadata") or {})
        user_story = (n.get("user_story") or meta.get("user_story") or "").strip()
        if user_story:
            meta["user_story"] = user_story
        node = PlanningNode(
            id=n["id"],
            domain=PlanningDomain.BACKEND,
            kind=kind,
            summary=n.get("summary", ""),
            details=n.get("details", ""),
            inputs=n.get("inputs", []),
            outputs=n.get("outputs", []),
            acceptance_criteria=n.get("acceptance_criteria", []),
            parent_id=n.get("parent_id"),
            metadata=meta,
        )
        graph.add_node(node)
    for e in data.get("edges") or []:
        if not isinstance(e, dict) or not e.get("from_id") or not e.get("to_id"):
            continue
        type_str = (e.get("type") or "blocks").lower()
        try:
            edge_type = EdgeType(type_str)
        except ValueError:
            edge_type = EdgeType.BLOCKS
        graph.add_edge(PlanningEdge(from_id=e["from_id"], to_id=e["to_id"], type=edge_type))
    return graph


class BackendPlanningAgent:
    """Produces backend-specific PlanningGraph from requirements and architecture."""

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def run(self, input_data: BackendPlanningInput) -> BackendPlanningOutput:
        """Generate backend planning graph."""
        logger.info("Backend Planning: starting for %s", input_data.requirements.title)
        reqs = input_data.requirements
        arch = input_data.architecture

        context_parts = [
            f"**Product Title:** {reqs.title}",
            f"**Description:** {reqs.description}",
            "**Acceptance Criteria:**",
            *[f"- {c}" for c in reqs.acceptance_criteria],
            "**Constraints:**",
            *[f"- {c}" for c in reqs.constraints],
        ]
        if input_data.project_overview:
            po = input_data.project_overview
            context_parts.extend([
                "",
                "**Project Overview:**",
                f"- Primary goal: {po.get('primary_goal', '')}",
                f"- Delivery strategy: {po.get('delivery_strategy', '')}",
            ])
        if arch:
            context_parts.extend([
                "",
                "**Architecture:**",
                arch.overview,
                "",
                "**Components (backend-relevant):**",
                *[f"- {c.name} ({c.type}): {c.description}" for c in arch.components if c.type in ("backend", "database", "api", "api_gateway")],
            ])
            if getattr(arch, "planning_hints", None):
                backend_hints = (arch.planning_hints or {}).get("backend") or {}
                hint_components = backend_hints.get("components") or []
                if hint_components:
                    context_parts.extend(
                        [
                            "",
                            "**Backend planning hints (from architecture):**",
                            "- Components to anchor tasks to: " + ", ".join(hint_components),
                        ]
                    )
        if input_data.spec_content:
            context_parts.extend([
                "",
                "**Spec (excerpt):**",
                (input_data.spec_content or "")[:8000] + ("..." if len(input_data.spec_content or "") > 8000 else ""),
            ])
        if input_data.codebase_analysis:
            context_parts.extend(["", "**Codebase Analysis:**", input_data.codebase_analysis[:4000]])
        if input_data.spec_analysis:
            context_parts.extend(["", "**Spec Analysis:**", input_data.spec_analysis[:4000]])

        prompt = BACKEND_PLANNING_PROMPT + "\n\n---\n\n" + "\n".join(context_parts)
        data = self.llm.complete_json(prompt, temperature=0.2)
        graph = _parse_graph_from_llm_output(data)
        logger.info("Backend Planning: produced %s nodes, %s edges", len(graph.nodes), len(graph.edges))
        return BackendPlanningOutput(planning_graph=graph, summary=data.get("summary", ""))
