"""Phase 5 — Governance & Evolution graph (pure fan-out).

Seven specialist agents produce governance fragments in parallel. Each is a
terminal node — there is no compositor; the orchestrator's Phase-5
``merge_fn`` (``_merge_phase5_fragments``) assembles their typed fragments
into a unified ``GovernanceOutput`` in Python.
"""

from __future__ import annotations

from strands.multiagent.graph import Graph, GraphBuilder

from branding_team.agents import (
    make_approval_workflow_designer,
    make_asset_wiki_planner,
    make_brand_rules_codifier,
    make_evolution_framer,
    make_kpi_designer,
    make_ownership_definer,
    make_training_planner,
)


def build_phase5_graph() -> Graph:
    """Build the Phase 5 Governance pure fan-out graph.

    Topology::

        ownership_definer
        approval_workflow_designer
        asset_wiki_planner
        training_planner
        kpi_designer
        evolution_framer
        brand_rules_codifier

    All seven nodes are both entry points and terminal nodes — they run in
    parallel and have no edges between them. There is no fan-in node: the
    orchestrator's Phase-5 ``merge_fn`` (``_merge_phase5_fragments``)
    assembles their typed ``structured_output`` fragments into a single
    ``GovernanceOutput`` deterministically in Python.

    Preconditions:
        None — the builder wires a fixed seven-agent factory set and takes no
        arguments.
    Postconditions:
        Returns a built ``Graph`` whose seven specialist nodes are all both
        entry points and terminal nodes, running in parallel with no edges
        between them. There is no fan-in node; the orchestrator's Phase-5
        ``merge_fn`` (``_merge_phase5_fragments``) assembles their typed
        ``structured_output`` fragments into a single ``GovernanceOutput``
        outside the graph.
    """
    builder = GraphBuilder()

    factories = {
        "ownership_definer": make_ownership_definer,
        "approval_workflow_designer": make_approval_workflow_designer,
        "asset_wiki_planner": make_asset_wiki_planner,
        "training_planner": make_training_planner,
        "kpi_designer": make_kpi_designer,
        "evolution_framer": make_evolution_framer,
        "brand_rules_codifier": make_brand_rules_codifier,
    }
    for node_id, factory in factories.items():
        builder.add_node(factory(), node_id=node_id)
        builder.set_entry_point(node_id)

    return builder.build()
