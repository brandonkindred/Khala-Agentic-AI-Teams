"""
PlanningGraph validation: cycle detection and orphan edge checks.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from .planning_graph import (
    EdgeType,
    PlanningGraph,
)


def validate_planning_graph(
    graph: PlanningGraph,
    requirement_count: int = 0,
) -> Tuple[bool, List[str]]:
    """
    Validate a PlanningGraph structurally. Returns (is_valid, list of error/warning messages).
    Only checks for cycles and dangling edges -- no hardcoded thresholds.
    """
    errors: List[str] = []
    nodes = graph.nodes
    edges = graph.edges

    blocks_edges = [(e.from_id, e.to_id) for e in edges if e.type == EdgeType.BLOCKS]
    if blocks_edges:
        cycle = _find_cycle(blocks_edges, set(nodes.keys()))
        if cycle:
            errors.append(f"Planning graph has dependency cycle: {' -> '.join(cycle)}")

    for e in edges:
        if e.type == EdgeType.VERIFIES and e.from_id in nodes and e.to_id not in nodes:
            errors.append(f"VERIFIES edge from {e.from_id} to non-existent node {e.to_id}")

    for e in edges:
        if e.type == EdgeType.DOCUMENTS and e.from_id in nodes and e.to_id not in nodes:
            errors.append(f"DOCUMENTS edge from {e.from_id} to non-existent node {e.to_id}")

    return len(errors) == 0, errors


def _find_cycle(edges: List[Tuple[str, str]], node_ids: set) -> Optional[List[str]]:
    """Find a cycle in the directed graph. Returns cycle path or None."""
    from_to: dict = {}
    for a, b in edges:
        if a in node_ids and b in node_ids:
            from_to.setdefault(a, []).append(b)
    visited = set()
    rec_stack = set()
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


def format_validation_report(
    is_valid: bool,
    errors: List[str],
    total_nodes: int,
    total_edges: int,
    domain_counts: dict,
) -> str:
    """Format a validation report."""
    lines = [
        "## Plan Validation Report",
        "",
        f"- **Status:** {'PASSED' if is_valid else 'ISSUES FOUND'}",
        f"- **Total nodes:** {total_nodes}",
        f"- **Total edges:** {total_edges}",
        f"- **Domain breakdown:** {domain_counts}",
        "",
    ]
    if errors:
        lines.extend(["### Issues", ""])
        for e in errors:
            lines.append(f"- {e}")
        lines.append("")
    return "\n".join(lines)
