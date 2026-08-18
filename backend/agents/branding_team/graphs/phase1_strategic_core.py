"""Phase 1 — Strategic Core graph (fan-out / fan-in).

Five specialist agents run in parallel to analyse the brand from different
angles, then a single positioning synthesizer merges their outputs into a
cohesive positioning statement and brand promise.
"""

from __future__ import annotations

from strands.multiagent.graph import Graph

from branding_team.agents import (
    make_audience_segmenter,
    make_differentiation_mapper,
    make_discovery_auditor,
    make_positioning_synthesizer,
    make_purpose_vision_writer,
    make_values_articulator,
)
from shared.graph import build_fan_out_fan_in


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

    Preconditions:
        None — wires a fixed set of agent factories and takes no arguments.
    Postconditions:
        Returns a built ``Graph`` whose five specialist nodes are entry points
        running in parallel and whose sole terminal node,
        ``positioning_synthesizer``, depends on all five (fan-out / fan-in).
    """
    return build_fan_out_fan_in(
        agents=[
            ("discovery_auditor", make_discovery_auditor()),
            ("purpose_vision_writer", make_purpose_vision_writer()),
            ("values_articulator", make_values_articulator()),
            ("audience_segmenter", make_audience_segmenter()),
            ("differentiation_mapper", make_differentiation_mapper()),
        ],
        compositor=("positioning_synthesizer", make_positioning_synthesizer()),
        graph_id="branding_phase1_strategic_core",
    )
