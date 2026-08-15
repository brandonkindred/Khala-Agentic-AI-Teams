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
from branding_team.graphs.shared import build_agent, build_fan_out_fan_in


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

    Preconditions:
        None — the builder wires a fixed nine-agent factory set and takes no
        arguments.
    Postconditions:
        Returns a built ``Graph`` whose nine specialist nodes are entry points
        running in parallel and whose sole terminal node, ``channel_compositor``,
        depends on all nine (fan-out / fan-in).
    """
    builder = GraphBuilder()

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

    # --- fan-out: independent channel / experience nodes, wired into compositor ---
    build_fan_out_fan_in(
        builder,
        [
            ("brand_experience_principler", make_brand_experience_principler),
            ("website_guide", make_website_guide),
            ("social_guide", make_social_guide),
            ("email_guide", make_email_guide),
            ("events_guide", make_events_guide),
            ("partnerships_guide", make_partnerships_guide),
            ("internal_guide", make_internal_guide),
            ("brand_architecture_builder", make_brand_architecture_builder),
            ("brand_in_action_illustrator", make_brand_in_action_illustrator),
        ],
        compositor,
    )

    return builder.build()
