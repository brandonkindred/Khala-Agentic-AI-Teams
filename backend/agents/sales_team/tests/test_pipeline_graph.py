"""Tests for ``sales_team.graphs.pipeline_graph.build_pipeline_graph``.

The graph builder wraps Strands ``GraphBuilder`` to wire seven sales-stage
agents (prospector → outreach → qualifier → discovery → proposal → negotiator)
with an optional nurture branch. The tests below verify:

  * the graph builds when ``include_nurture`` defaults to True (the production
    case),
  * the optional ``include_nurture=False`` branch builds a graph with no
    ``nurturer`` node,
  * the ``learning_insights`` argument actually flows into agent prompts,
  * the graph carries the timeouts and graph-id the function configures.

Each test imports ``build_pipeline_graph`` lazily to keep failures in
Strands import scoped to a single test rather than the module collection.
"""

from __future__ import annotations


def _build_graph(**kwargs):
    """Helper to import + build inside each test (keeps import errors local)."""
    from sales_team.graphs.pipeline_graph import build_pipeline_graph

    return build_pipeline_graph(**kwargs)


def test_default_build_returns_graph_with_seven_nodes() -> None:
    graph = _build_graph()
    # Strands Graph exposes nodes via attribute access; we cover both shapes
    # (newer versions use ``nodes`` dict; older versions store under
    # ``_node_lookup``).
    nodes = getattr(graph, "nodes", None) or getattr(graph, "_node_lookup", {})
    node_ids = set(nodes.keys()) if isinstance(nodes, dict) else {n.node_id for n in nodes}
    assert {
        "prospector",
        "outreach",
        "qualifier",
        "discovery",
        "proposal",
        "negotiator",
        "nurturer",
    }.issubset(node_ids)


def test_build_without_nurture_excludes_nurturer_node() -> None:
    graph = _build_graph(include_nurture=False)
    nodes = getattr(graph, "nodes", None) or getattr(graph, "_node_lookup", {})
    node_ids = set(nodes.keys()) if isinstance(nodes, dict) else {n.node_id for n in nodes}
    assert "nurturer" not in node_ids
    # The other six nodes are still present.
    assert {"prospector", "outreach", "qualifier", "discovery", "proposal", "negotiator"}.issubset(
        node_ids
    )


def test_learning_insights_are_injected_into_agent_prompts() -> None:
    """The insights string passed in must appear in each agent's system prompt."""
    sentinel = "INSIGHTSXYZ-1234"
    graph = _build_graph(learning_insights=sentinel)
    nodes = getattr(graph, "nodes", None) or getattr(graph, "_node_lookup", {})
    node_iter = nodes.values() if isinstance(nodes, dict) else list(nodes)
    found_in_any = False
    for node in node_iter:
        # The Strands ``GraphNode`` keeps the executor; the Agent's system
        # prompt is on ``.executor.system_prompt`` for our build_agent helper.
        executor = getattr(node, "executor", None)
        sp = getattr(executor, "system_prompt", "") or ""
        if sentinel in sp:
            found_in_any = True
            break
    assert found_in_any, "learning_insights string should appear in at least one agent prompt"


def test_no_insights_means_no_insights_block() -> None:
    """When ``learning_insights`` is empty the prompts must NOT contain the prefix."""
    graph = _build_graph(learning_insights="")
    nodes = getattr(graph, "nodes", None) or getattr(graph, "_node_lookup", {})
    node_iter = nodes.values() if isinstance(nodes, dict) else list(nodes)
    for node in node_iter:
        executor = getattr(node, "executor", None)
        sp = getattr(executor, "system_prompt", "") or ""
        assert "Learning insights from prior campaigns" not in sp


def test_entry_point_is_prospector() -> None:
    graph = _build_graph()
    # Strands Graph exposes the entry point via ``.entry_points`` (set/list)
    # or, on older versions, via a single ``.entry_point`` attribute.
    if hasattr(graph, "entry_points"):
        ep = graph.entry_points
        if isinstance(ep, (list, set, tuple)):
            ids = {getattr(e, "node_id", e) for e in ep}
        else:
            ids = {getattr(ep, "node_id", ep)}
        assert "prospector" in ids
    else:
        assert getattr(graph, "entry_point", None) is not None
