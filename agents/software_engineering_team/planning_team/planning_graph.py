"""
PlanningGraph: internal planning representation used by planning agents.

The graph is compiled down to the existing TaskAssignment format for compatibility
with execution agents (BackendExpertAgent, FrontendExpertAgent, etc.).
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from shared.models import Task, TaskAssignment, TaskStatus, TaskType

logger = logging.getLogger(__name__)


def ensure_str_list(val: Any) -> List[str]:
    """Coerce LLM output to List[str] for PlanningNode list fields. Handles string, None, list, iterable."""
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
    """Coerce LLM output to Dict for PlanningNode metadata. Handles None, non-dict."""
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

    EPIC = "epic"
    FEATURE = "feature"
    TASK = "task"
    SUBTASK = "subtask"


class EdgeType(str, Enum):
    """Type of dependency between planning nodes."""

    BLOCKS = "blocks"  # from_id must complete before to_id
    RELATES_TO = "relates_to"
    VERIFIES = "verifies"  # test/docs node verifies a feature
    DOCUMENTS = "documents"
    LOADS_FROM = "loads_from"
    EXPOSES_API = "exposes_api"


class Phase(BaseModel):
    """A phase in the development plan (e.g., Scaffolding, Core Features)."""

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
    parent_id: Optional[str] = None  # For hierarchical structure
    metadata: Dict[str, Any] = Field(default_factory=dict)
    # Quality gate config (from QualityGatePlanningAgent)
    quality_gates: List[str] = Field(default_factory=list)
    # Performance budget (from PerformancePlanningAgent)
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


def _domain_to_task_type(domain: PlanningDomain) -> TaskType:
    """Map planning domain to TaskType for execution."""
    mapping = {
        PlanningDomain.BACKEND: TaskType.BACKEND,
        PlanningDomain.FRONTEND: TaskType.FRONTEND,
        PlanningDomain.DATA: TaskType.BACKEND,  # Data tasks typically go to backend
        PlanningDomain.QA: TaskType.QA,
        PlanningDomain.DOCS: TaskType.DOCUMENTATION,
        PlanningDomain.PERF: TaskType.BACKEND,  # Perf tasks often backend
        PlanningDomain.DEVOPS: TaskType.DEVOPS,
        PlanningDomain.GIT_SETUP: TaskType.GIT_SETUP,
    }
    return mapping.get(domain, TaskType.BACKEND)


def _domain_to_assignee(domain: PlanningDomain) -> str:
    """Map planning domain to assignee string."""
    return domain.value


def _find_cycle(edges: List[tuple], node_ids: set) -> Optional[List[str]]:
    """Find a cycle in the directed graph. Returns cycle path (e.g. [a,b,c,a]) or None."""
    from_to: Dict[str, List[str]] = {}
    for a, b in edges:
        if a in node_ids and b in node_ids:
            from_to.setdefault(a, []).append(b)
    visited: set = set()
    rec_stack: set = set()
    path: List[str] = []
    cycle_path: List[str] = []

    def dfs(u: str) -> bool:
        visited.add(u)
        rec_stack.add(u)
        path.append(u)
        for v in from_to.get(u, []):
            if v not in visited:
                if dfs(v):
                    return True
            elif v in rec_stack:
                idx = path.index(v)
                cycle_path.extend(path[idx:] + [v])
                return True
        path.pop()
        rec_stack.discard(u)
        return False

    for nid in node_ids:
        if nid not in visited and dfs(nid):
            return cycle_path
    return None


def _break_cycles_in_blocks_edges(
    edges: List[PlanningEdge],
    nodes: Dict[str, PlanningNode],
) -> List[tuple]:
    """Return BLOCKS edges as (from_id, to_id) tuples with cycles broken by removing one edge per cycle."""
    node_ids = set(nodes.keys())
    blocks_edges = [
        (e.from_id, e.to_id)
        for e in edges
        if e.type == EdgeType.BLOCKS and e.from_id in node_ids and e.to_id in node_ids
    ]
    blocks_list = list(blocks_edges)  # mutable copy
    while True:
        cycle = _find_cycle(blocks_list, node_ids)
        if not cycle or len(cycle) < 3:
            break
        # Remove one edge from the cycle: (cycle[-2], cycle[-1])
        edge_to_remove = (cycle[-2], cycle[-1])
        if edge_to_remove in blocks_list:
            blocks_list.remove(edge_to_remove)
        else:
            break
    return blocks_list


def _topological_order(
    nodes: Dict[str, PlanningNode],
    edges: List[PlanningEdge],
    domain_balance: bool = True,
) -> List[str]:
    """
    Compute execution order respecting BLOCKS edges.
    Uses Kahn's algorithm. Optionally interleaves backend/frontend for domain balance.
    Breaks cycles in BLOCKS edges by removing one edge per cycle.
    """
    # Break cycles so topological sort succeeds for all nodes
    blocks_edges = _break_cycles_in_blocks_edges(edges, nodes)
    # Build adjacency: from_id -> [to_id] for BLOCKS edges
    blocks_from: Dict[str, List[str]] = {nid: [] for nid in nodes}
    blocks_to: Dict[str, List[str]] = {nid: [] for nid in nodes}
    for from_id, to_id in blocks_edges:
        blocks_from[from_id].append(to_id)
        blocks_to[to_id].append(from_id)

    # In-degree = number of BLOCKS predecessors
    in_degree = {nid: len(blocks_to[nid]) for nid in nodes}

    # Only TASK and SUBTASK nodes go into execution order
    executable_ids = [
        nid for nid, n in nodes.items()
        if n.kind in (PlanningNodeKind.TASK, PlanningNodeKind.SUBTASK)
    ]
    executable_set = set(executable_ids)

    # Kahn's algorithm
    queue = [nid for nid in executable_ids if in_degree[nid] == 0]
    result: List[str] = []
    while queue:
        # If domain_balance, prefer alternating backend/frontend
        if domain_balance and len(queue) > 1:
            backend_candidates = [
                nid for nid in queue
                if nodes[nid].domain in (PlanningDomain.BACKEND, PlanningDomain.DATA, PlanningDomain.PERF)
            ]
            frontend_candidates = [
                nid for nid in queue
                if nodes[nid].domain == PlanningDomain.FRONTEND
            ]
            prefix_candidates = [
                nid for nid in queue
                if nodes[nid].domain in (PlanningDomain.GIT_SETUP, PlanningDomain.DEVOPS)
            ]
            # Process prefix first, then interleave backend/frontend
            if prefix_candidates:
                nid = prefix_candidates[0]
            elif result:
                last_domain = nodes.get(result[-1], PlanningNode(id="", domain=PlanningDomain.BACKEND, kind=PlanningNodeKind.TASK)).domain
                if last_domain in (PlanningDomain.BACKEND, PlanningDomain.DATA, PlanningDomain.PERF) and frontend_candidates:
                    nid = frontend_candidates[0]
                elif last_domain == PlanningDomain.FRONTEND and backend_candidates:
                    nid = backend_candidates[0]
                else:
                    nid = queue[0]
            else:
                # Prefer backend first if available
                nid = backend_candidates[0] if backend_candidates else queue[0]
        else:
            nid = queue[0]

        queue.remove(nid)
        result.append(nid)
        for succ in blocks_from.get(nid, []):
            if succ in executable_set:
                in_degree[succ] -= 1
                if in_degree[succ] == 0:
                    queue.append(succ)

    # Append any executable nodes not reached (disconnected; cycles are broken above)
    for nid in executable_ids:
        if nid not in result:
            result.append(nid)
            logger.info("PlanningGraph: node %s not in topological order (disconnected)", nid)

    return result


def _is_overly_broad_task(details: str, summary: str) -> bool:
    """Return True if task details/summary suggest a monolithic scope (4+ and-separated items).

    E.g. 'models and endpoints and validation and error handling' indicates the task
    should be split. Used to reject/flag tasks that would cause backend agent to exceed cycles.
    """
    text = (details or "") + " " + (summary or "")
    if not text.strip():
        return False
    # Split on " and " (with optional commas) to count distinct scope items
    parts = [p.strip() for p in text.replace(",", " and ").split(" and ") if p.strip()]
    return len(parts) >= 4


def compile_planning_graph_to_task_assignment(
    graph: PlanningGraph,
    rationale: str = "",
) -> TaskAssignment:
    """
    Compile a PlanningGraph into a TaskAssignment for execution agents.

    - EPIC/FEATURE nodes are collapsed into metadata/tags.
    - TASK/SUBTASK nodes become Task objects.
    - Execution order is topologically sorted with backend/frontend interleaving.
    - Overly broad tasks (4+ and-separated items in details) are skipped and logged.
    """
    tasks: List[Task] = []
    task_ids_seen: set = set()

    for nid, node in graph.nodes.items():
        if node.kind not in (PlanningNodeKind.TASK, PlanningNodeKind.SUBTASK):
            continue
        if nid in task_ids_seen:
            continue
        task_ids_seen.add(nid)

        # Skip overly broad tasks that would cause backend agent to exceed cycles
        if _is_overly_broad_task(node.details or "", node.summary or ""):
            logger.warning(
                "PlanningGraph: skipping overly broad task %s (details suggests 4+ scope items). "
                "Ask planner to split into granular tasks.",
                nid,
            )
            continue

        task_type = _domain_to_task_type(node.domain)
        assignee = _domain_to_assignee(node.domain)

        # Skip QA and standalone security - orchestrator invokes those
        if task_type == TaskType.QA:
            continue

        description = node.details or node.summary
        if len(description) < 50:
            description = f"{node.summary}\n\n{description}".strip() or f"Implement {node.summary}"

        acceptance_criteria = node.acceptance_criteria or []
        if len(acceptance_criteria) < 3 and node.summary:
            acceptance_criteria.extend([
                f"Deliver {node.summary}",
                "Code follows architecture and spec",
                "All quality gates pass",
            ])

        # Build dependencies from BLOCKS edges (who blocks us)
        dependencies = []
        for e in graph.edges:
            if e.to_id == nid and e.type == EdgeType.BLOCKS:
                if e.from_id in graph.nodes and graph.nodes[e.from_id].kind in (PlanningNodeKind.TASK, PlanningNodeKind.SUBTASK):
                    dependencies.append(e.from_id)

        # Quality gate info in requirements if present
        requirements = node.details or ""
        if node.quality_gates:
            requirements += f"\n\nQuality gates: {', '.join(node.quality_gates)}"
        if node.performance_budget:
            requirements += f"\n\nPerformance budget: {node.performance_budget}"

        user_story = (node.metadata.get("user_story") or "").strip()
        if not user_story and task_type in (TaskType.BACKEND, TaskType.FRONTEND):
            summary = (node.summary or nid).strip()
            user_story = f"As a developer, I want {summary} so that the system meets the requirements."

        tasks.append(
            Task(
                id=nid,
                type=task_type,
                title=node.summary[:80] if node.summary else nid,
                description=description,
                user_story=user_story,
                assignee=assignee,
                requirements=requirements.strip(),
                dependencies=dependencies,
                acceptance_criteria=acceptance_criteria[:7],
                status=TaskStatus.PENDING,
                metadata=node.metadata or {},
            )
        )

    execution_order = _topological_order(graph.nodes, graph.edges, domain_balance=True)

    # Filter execution_order to only include task IDs we emitted
    valid_ids = {t.id for t in tasks}
    execution_order = [tid for tid in execution_order if tid in valid_ids]

    # Add any tasks not in execution_order (e.g. docs, perf that we kept)
    for t in tasks:
        if t.id not in execution_order:
            execution_order.append(t.id)

    return TaskAssignment(
        tasks=tasks,
        execution_order=execution_order,
        rationale=rationale,
    )
