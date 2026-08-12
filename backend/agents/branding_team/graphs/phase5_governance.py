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
from branding_team.graphs.shared import build_compositor, build_fan_out_fan_in


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

    # ── Fan-in: governance compositor ───────────────────────────────
    compositor_agent = build_compositor(
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

    # ── Fan-out: parallel specialist nodes, wired into compositor ───
    build_fan_out_fan_in(
        builder,
        [
            ("ownership_definer", make_ownership_definer),
            ("approval_workflow_designer", make_approval_workflow_designer),
            ("asset_wiki_planner", make_asset_wiki_planner),
            ("training_planner", make_training_planner),
            ("kpi_designer", make_kpi_designer),
            ("evolution_framer", make_evolution_framer),
            ("brand_rules_codifier", make_brand_rules_codifier),
        ],
        compositor,
    )

    return builder.build()
