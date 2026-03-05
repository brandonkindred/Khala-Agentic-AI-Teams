"""
PlanningGraph: internal planning representation used by planning agents.

Retained as a structural model for agents that produce graph-based planning
artifacts. No heuristic compilation -- the Initiative/Epic/Story hierarchy
in shared.models is the primary planning output format.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


def ensure_str_list(val: Any) -> List[str]:
    """Coerce LLM output to List[str] for PlanningNode list fields."""
    if val is None:
        return []
    if isinstance(val, str):
        return [val] if val.strip() else []
    if isinstance(val, list):
        return [str(x) for x in val if x is not None]
    try:
        return [str(x) for x in val if x is not None]
    except (TypeError, ValueError):
        return []


def ensure_dict(val: Any) -> Dict[str, Any]:
    """Coerce LLM output to Dict for PlanningNode metadata."""
    if val is None:
        return {}
    if isinstance(val, dict):
        return dict(val)
    return {}


class PlanningDomain(str, Enum):
    """Domain that owns a planning node."""

    BACKEND = "backend"
    FRONTEND = "frontend"
    DATA = "data"
    QA = "qa"
    DOCS = "docs"
    PERF = "perf"
    DEVOPS = "devops"
    GIT_SETUP = "git_setup"


class PlanningNodeKind(str, Enum):
    """Hierarchy level of a planning node."""

    INITIATIVE = "initiative"
    EPIC = "epic"
    STORY = "story"


class EdgeType(str, Enum):
    """Type of dependency between planning nodes."""

    BLOCKS = "blocks"
    RELATES_TO = "relates_to"
    VERIFIES = "verifies"
    DOCUMENTS = "documents"
    LOADS_FROM = "loads_from"
    EXPOSES_API = "exposes_api"


class Phase(BaseModel):
    """A phase in the development plan."""

    id: str
    name: str
    description: str = ""
    node_ids: List[str] = Field(default_factory=list)


class PlanningNode(BaseModel):
    """A node in the planning graph."""

    id: str
    domain: PlanningDomain
    kind: PlanningNodeKind
    summary: str = ""
    details: str = ""
    inputs: List[str] = Field(default_factory=list)
    outputs: List[str] = Field(default_factory=list)
    acceptance_criteria: List[str] = Field(default_factory=list)
    parent_id: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    quality_gates: List[str] = Field(default_factory=list)
    performance_budget: Optional[str] = None


class PlanningEdge(BaseModel):
    """An edge between two planning nodes."""

    from_id: str
    to_id: str
    type: EdgeType = EdgeType.BLOCKS


class PlanningGraph(BaseModel):
    """Full planning graph with nodes, edges, and phases."""

    nodes: Dict[str, PlanningNode] = Field(default_factory=dict)
    edges: List[PlanningEdge] = Field(default_factory=list)
    phases: List[Phase] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    def add_node(self, node: PlanningNode) -> None:
        """Add or replace a node."""
        self.nodes[node.id] = node

    def add_edge(self, edge: PlanningEdge) -> None:
        """Add an edge."""
        self.edges.append(edge)

    def merge(self, other: PlanningGraph) -> None:
        """Merge another graph into this one. Nodes are overwritten by id."""
        for nid, node in other.nodes.items():
            self.nodes[nid] = node
        self.edges.extend(other.edges)
        for phase in other.phases:
            if not any(p.id == phase.id for p in self.phases):
                self.phases.append(phase)
        self.metadata.update(other.metadata)
