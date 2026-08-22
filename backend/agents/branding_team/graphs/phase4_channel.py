"""Phase 4 — Channel Activation graph (pure fan-out).

Nine specialist agents produce channel-specific guidelines and brand
experience artefacts in parallel. Each is a terminal node — there is no
compositor; the orchestrator's Phase-4 ``merge_fn``
(``_merge_phase4_fragments``) assembles their typed fragments into a
unified ``ChannelActivationOutput`` in Python.
"""

from __future__ import annotations

from strands.multiagent.graph import Graph, GraphBuilder

from branding_team.agents import (
    CHANNEL_SPECS,
    _make_channel_guide,
    make_brand_architecture_builder,
    make_brand_experience_principler,
    make_brand_in_action_illustrator,
)
from branding_team.models import ChannelGuidelineOutput


def build_phase4_graph() -> Graph:
    """Build the Phase 4 Channel Activation pure fan-out graph.

    Topology::

        brand_experience_principler
        website_guide
        social_guide
        email_guide
        events_guide
        partnerships_guide
        internal_guide
        brand_architecture_builder
        brand_in_action_illustrator

    All nine nodes are both entry points and terminal nodes — they run in
    parallel and have no edges between them. There is no fan-in node: the
    orchestrator's Phase-4 ``merge_fn`` (``_merge_phase4_fragments``)
    assembles their typed ``structured_output`` fragments into a single
    ``ChannelActivationOutput`` deterministically in Python.

    Preconditions:
        None — the builder wires a fixed nine-agent factory set and takes no
        arguments.
    Postconditions:
        Returns a built ``Graph`` whose nine specialist nodes are all both entry
        points and terminal nodes, running in parallel with no edges between them.
        There is no fan-in node; the orchestrator's Phase-4 ``merge_fn``
        (``_merge_phase4_fragments``) assembles their typed ``structured_output``
        fragments into a single ``ChannelActivationOutput`` outside the graph.
    """
    builder = GraphBuilder()

    non_channel_factories = {
        "brand_experience_principler": make_brand_experience_principler,
        "brand_architecture_builder": make_brand_architecture_builder,
        "brand_in_action_illustrator": make_brand_in_action_illustrator,
    }
    for node_id, factory in non_channel_factories.items():
        builder.add_node(factory(), node_id=node_id)
        builder.set_entry_point(node_id)

    for channel, description in CHANNEL_SPECS:
        node_id = f"{channel}_guide"
        builder.add_node(
            _make_channel_guide(channel, description, ChannelGuidelineOutput),
            node_id=node_id,
        )
        builder.set_entry_point(node_id)

    return builder.build()
