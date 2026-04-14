"""Phase 1 — Strategic Core graph (fan-out / fan-in).

Five specialist agents run in parallel to analyse the brand from different
angles, then a single positioning synthesizer merges their outputs into a
cohesive positioning statement and brand promise.
"""

from __future__ import annotations

from strands.multiagent.graph import Graph, GraphBuilder

from branding_team.agents import (
    make_audience_segmenter,
    make_differentiation_mapper,
    make_discovery_auditor,
    make_positioning_synthesizer,
    make_purpose_vision_writer,
    make_values_articulator,
)


def build_phase1_graph() -> Graph:
    """Build the Phase 1 Strategic Core fan-out/fan-in graph.

    Topology::

        discovery_auditor ──────────┐
        purpose_vision_writer ──────┤
        values_articulator ─────────┼──▶ positioning_synthesizer
        audience_segmenter ─────────┤
        differentiation_mapper ─────┘

    The five entry nodes execute in parallel with no inter-dependencies.
    ``positioning_synthesizer`` runs once all five have completed and
    synthesises their outputs into a positioning statement and brand promise.

    Returns
    -------
    Graph
        A callable ``Graph`` instance.
    """
    builder = GraphBuilder()

    # --- fan-out: independent specialist nodes ---
    discovery = builder.add_node(make_discovery_auditor(), node_id="discovery_auditor")
    purpose = builder.add_node(make_purpose_vision_writer(), node_id="purpose_vision_writer")
    values = builder.add_node(make_values_articulator(), node_id="values_articulator")
    audience = builder.add_node(make_audience_segmenter(), node_id="audience_segmenter")
    diffmap = builder.add_node(make_differentiation_mapper(), node_id="differentiation_mapper")

    # --- fan-in: synthesizer depends on all five ---
    synthesizer = builder.add_node(
        make_positioning_synthesizer(), node_id="positioning_synthesizer"
    )

    builder.add_edge(discovery, synthesizer)
    builder.add_edge(purpose, synthesizer)
    builder.add_edge(values, synthesizer)
    builder.add_edge(audience, synthesizer)
    builder.add_edge(diffmap, synthesizer)

    # --- all fan-out nodes are entry points (parallel start) ---
    builder.set_entry_point("discovery_auditor")
    builder.set_entry_point("purpose_vision_writer")
    builder.set_entry_point("values_articulator")
    builder.set_entry_point("audience_segmenter")
    builder.set_entry_point("differentiation_mapper")

    return builder.build()
