"""Phase 5 — Governance & Evolution graph (fan-out / fan-in).

Seven specialist agents run in parallel to produce governance fragments,
then a Governance Compositor joins the results into a unified output.
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
from branding_team.graphs.shared import build_agent


def build_phase5_graph() -> Graph:
    """Build the Phase 5 Governance fan-out/fan-in graph.

    Entry nodes (all run in parallel):
        ownership_definer, approval_workflow_designer, asset_wiki_planner,
        training_planner, kpi_designer, evolution_framer, brand_rules_codifier

    Join node:
        governance_compositor — assembles every upstream fragment into a
        single GovernanceOutput JSON document.

    Returns:
        A compiled ``Graph`` ready for invocation.
    """
    builder = GraphBuilder()

    # ── Fan-out: parallel specialist nodes ──────────────────────────
    ownership = builder.add_node(make_ownership_definer(), node_id="ownership_definer")
    approval = builder.add_node(
        make_approval_workflow_designer(), node_id="approval_workflow_designer"
    )
    wiki = builder.add_node(make_asset_wiki_planner(), node_id="asset_wiki_planner")
    training = builder.add_node(make_training_planner(), node_id="training_planner")
    kpi = builder.add_node(make_kpi_designer(), node_id="kpi_designer")
    evolution = builder.add_node(make_evolution_framer(), node_id="evolution_framer")
    rules = builder.add_node(make_brand_rules_codifier(), node_id="brand_rules_codifier")

    # ── Fan-in: governance compositor ───────────────────────────────
    compositor_agent = build_agent(
        name="governance_compositor",
        system_prompt=(
            "You are a Governance Compositor. Assemble all governance fragments from upstream agents "
            "into a unified GovernanceOutput. Combine ownership model, decision authority, approval "
            "workflows, agency briefing protocols, asset management guidance, training plan, brand "
            "health KPIs, tracking methodology, review triggers, evolution framework, version control "
            "cadence, brand guidelines list, and wiki backlog. Output comprehensive valid JSON."
        ),
        description="Joins all governance fragments into a single GovernanceOutput document.",
    )
    compositor = builder.add_node(compositor_agent, node_id="governance_compositor")

    # ── Entry points (all specialists start in parallel) ────────────
    builder.set_entry_point("ownership_definer")
    builder.set_entry_point("approval_workflow_designer")
    builder.set_entry_point("asset_wiki_planner")
    builder.set_entry_point("training_planner")
    builder.set_entry_point("kpi_designer")
    builder.set_entry_point("evolution_framer")
    builder.set_entry_point("brand_rules_codifier")

    # ── Edges: every specialist feeds into the compositor ───────────
    builder.add_edge(ownership, compositor)
    builder.add_edge(approval, compositor)
    builder.add_edge(wiki, compositor)
    builder.add_edge(training, compositor)
    builder.add_edge(kpi, compositor)
    builder.add_edge(evolution, compositor)
    builder.add_edge(rules, compositor)

    return builder.build()
