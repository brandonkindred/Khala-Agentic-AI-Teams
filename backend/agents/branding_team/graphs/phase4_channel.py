"""Phase 4 — Channel Activation graph (fan-out / fan-in).

Nine specialist agents produce channel-specific guidelines and brand
experience artefacts in parallel; a compositor node assembles them into a
unified ``ChannelActivationOutput``.
"""

from __future__ import annotations

from strands.multiagent.graph import Graph, GraphBuilder

from branding_team.agents import (
    make_brand_architecture_builder,
    make_brand_experience_principler,
    make_brand_in_action_illustrator,
    make_email_guide,
    make_events_guide,
    make_internal_guide,
    make_partnerships_guide,
    make_social_guide,
    make_website_guide,
)
from branding_team.graphs.shared import build_agent


def build_phase4_graph() -> Graph:
    """Build the Phase 4 Channel Activation fan-out/fan-in graph.

    Topology::

        brand_experience_principler ──┐
        website_guide ────────────────┤
        social_guide ─────────────────┤
        email_guide ──────────────────┤
        events_guide ─────────────────┼──▶ channel_compositor
        partnerships_guide ───────────┤
        internal_guide ───────────────┤
        brand_architecture_builder ───┤
        brand_in_action_illustrator ──┘

    All nine entry nodes execute in parallel.  ``channel_compositor`` runs
    once every entry node has completed and merges their outputs into a
    single unified channel-activation deliverable.

    Returns
    -------
    Graph
        A callable ``Graph`` instance.
    """
    builder = GraphBuilder()

    # --- fan-out: independent channel / experience nodes ---
    experience = builder.add_node(
        make_brand_experience_principler(), node_id="brand_experience_principler"
    )
    website = builder.add_node(make_website_guide(), node_id="website_guide")
    social = builder.add_node(make_social_guide(), node_id="social_guide")
    email = builder.add_node(make_email_guide(), node_id="email_guide")
    events = builder.add_node(make_events_guide(), node_id="events_guide")
    partnerships = builder.add_node(make_partnerships_guide(), node_id="partnerships_guide")
    internal = builder.add_node(make_internal_guide(), node_id="internal_guide")
    architecture = builder.add_node(
        make_brand_architecture_builder(), node_id="brand_architecture_builder"
    )
    in_action = builder.add_node(
        make_brand_in_action_illustrator(), node_id="brand_in_action_illustrator"
    )

    # --- fan-in: compositor assembles all channel outputs ---
    compositor = builder.add_node(
        build_agent(
            name="channel_compositor",
            description="Assembles all channel and experience outputs into a unified deliverable.",
            system_prompt=(
                "You are a Channel Activation Compositor. You receive outputs from nine specialist "
                "agents: brand experience principles, website guidelines, social media guidelines, "
                "email guidelines, events guidelines, partnerships guidelines, internal communications "
                "guidelines, brand architecture definitions, and brand-in-action examples.\n\n"
                "Your job is to assemble all of these into a single unified ChannelActivationOutput. "
                "Ensure consistency across channels, resolve any contradictions, and produce a "
                "coherent document that covers:\n"
                "- brand_experience_principles\n"
                "- channel_guidelines (list of per-channel guideline objects)\n"
                "- brand_architecture\n"
                "- brand_in_action_examples\n\n"
                "Output valid JSON matching the ChannelActivationOutput schema."
            ),
        ),
        node_id="channel_compositor",
    )

    # --- edges: every entry node feeds into the compositor ---
    entry_nodes = [
        experience,
        website,
        social,
        email,
        events,
        partnerships,
        internal,
        architecture,
        in_action,
    ]
    for node in entry_nodes:
        builder.add_edge(node, compositor)

    # --- all fan-out nodes are entry points (parallel start) ---
    builder.set_entry_point("brand_experience_principler")
    builder.set_entry_point("website_guide")
    builder.set_entry_point("social_guide")
    builder.set_entry_point("email_guide")
    builder.set_entry_point("events_guide")
    builder.set_entry_point("partnerships_guide")
    builder.set_entry_point("internal_guide")
    builder.set_entry_point("brand_architecture_builder")
    builder.set_entry_point("brand_in_action_illustrator")

    return builder.build()
