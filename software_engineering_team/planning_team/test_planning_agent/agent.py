"""Test Planning agent: adds test tasks with VERIFIES edges."""

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

from .models import TestPlanningInput, TestPlanningOutput
from .prompts import TEST_PLANNING_PROMPT

logger = logging.getLogger(__name__)


def _parse_graph_from_llm_output(data: Dict[str, Any], existing_task_ids: list) -> PlanningGraph:
    """Parse LLM JSON output into PlanningGraph."""
    graph = PlanningGraph()
    for n in data.get("nodes") or []:
        if not isinstance(n, dict) or not n.get("id"):
            continue
        domain_str = (n.get("domain") or "backend").lower()
        try:
            domain = PlanningDomain(domain_str)
        except ValueError:
            domain = PlanningDomain.BACKEND
        kind_str = (n.get("kind") or "task").lower()
        try:
            kind = PlanningNodeKind(kind_str)
        except ValueError:
            kind = PlanningNodeKind.TASK
        node = PlanningNode(
            id=n["id"],
            domain=domain,
            kind=kind,
            summary=n.get("summary", ""),
            details=n.get("details", ""),
            acceptance_criteria=n.get("acceptance_criteria", []),
            metadata=n.get("metadata", {}),
        )
        graph.add_node(node)
    for e in data.get("edges") or []:
        if not isinstance(e, dict) or not e.get("from_id") or not e.get("to_id"):
            continue
        type_str = (e.get("type") or "verifies").lower()
        try:
            edge_type = EdgeType(type_str)
        except ValueError:
            edge_type = EdgeType.VERIFIES
        graph.add_edge(PlanningEdge(from_id=e["from_id"], to_id=e["to_id"], type=edge_type))
    return graph


class TestPlanningAgent:
    """Adds test tasks that verify feature tasks."""

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def run(self, input_data: TestPlanningInput) -> TestPlanningOutput:
        """Generate test planning graph."""
        if not input_data.existing_task_ids:
            logger.info("Test Planning: no existing tasks, skipping")
            return TestPlanningOutput(planning_graph=PlanningGraph(), summary="No feature tasks to verify")

        logger.info("Test Planning: starting for %s (%s feature tasks)", input_data.requirements.title, len(input_data.existing_task_ids))
        reqs = input_data.requirements
        arch = input_data.architecture

        context_parts = [
            f"**Product Title:** {reqs.title}",
            "**Acceptance Criteria:**",
            *[f"- {c}" for c in reqs.acceptance_criteria],
            "",
            "**Existing feature task IDs to verify:**",
            ", ".join(input_data.existing_task_ids),
        ]
        if arch:
            context_parts.extend(["", "**Architecture:**", arch.overview[:2000]])
        if input_data.spec_content:
            context_parts.extend(["", "**Spec (excerpt):**", (input_data.spec_content or "")[:4000]])

        prompt = TEST_PLANNING_PROMPT + "\n\n---\n\n" + "\n".join(context_parts)
        data = self.llm.complete_json(prompt, temperature=0.2)
        graph = _parse_graph_from_llm_output(data, input_data.existing_task_ids)
        logger.info("Test Planning: produced %s nodes, %s edges", len(graph.nodes), len(graph.edges))
        return TestPlanningOutput(planning_graph=graph, summary=data.get("summary", ""))
