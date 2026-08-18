"""Phase 3 -- Visual & Expressive Identity (Graph).

Three MoodBoardConceptualist variants fan out in parallel directly into
converge_decider, which then fans out into seven visual specialists. The
diverge fan-out no longer routes through an intermediate CreativeDirector
collector agent: Strands' Graph engine assembles all three completed
predecessors' outputs into converge_decider's input deterministically (in
Python, no LLM call needed) once every entry-point node in the diverge batch
has finished. The seven post-converge specialists are terminal nodes -- there
is no compositor; the orchestrator's Phase-3 ``merge_fn``
(``_merge_phase3_fragments``) assembles their typed fragments, plus
converge_decider's own decision and the three moodboard candidates, into a
unified ``VisualIdentityOutput`` in Python.

This is intentionally a Graph rather than a Swarm. Agents built with
``structured_output=`` force Strands' structured-output tool and then
stop the agent loop (``stop_loop=True``), so they never call
``handoff_to_agent``. The diverge step therefore wires a direct multi-agent
fan-in (``shared.graph.wire_fan_out_fan_in``) with no intermediate collector
node, then keeps building onto the same graph for the post-converge
fan-out below -- unlike Phase 1, which is nothing but a fan-out/fan-in and
so builds its whole graph via ``shared.graph.build_fan_out_fan_in`` directly.

Pattern::

    MoodBoardConceptualist_Editorial ──┐
    MoodBoardConceptualist_Minimalist ─┼──▶ converge_decider
    MoodBoardConceptualist_Bold ───────┘         │
                                                 +--> logo_specifier
                                                 +--> color_system_builder
                                                 +--> typography_builder
                                                 +--> iconography_director
                                                 +--> photography_video_director
                                                 +--> voice_tone_builder
                                                 +--> design_system_codifier
"""

from __future__ import annotations

from strands.multiagent.graph import Graph, GraphBuilder

from branding_team.agents import (
    make_color_system_builder,
    make_converge_decider,
    make_design_system_codifier,
    make_iconography_director,
    make_logo_specifier,
    make_moodboard_conceptualist,
    make_photography_video_director,
    make_typography_builder,
    make_voice_tone_builder,
)
from shared.graph import wire_fan_out_fan_in

_PHASE3_CONCEPTUALIST_VARIANTS: tuple[str, ...] = ("Editorial", "Minimalist", "Bold")

# Insertion order is the specialist fan-out sequence (and the test source of truth).
_PHASE3_SPECIALIST_FACTORIES = {
    "logo_specifier": make_logo_specifier,
    "color_system_builder": make_color_system_builder,
    "typography_builder": make_typography_builder,
    "iconography_director": make_iconography_director,
    "photography_video_director": make_photography_video_director,
    "voice_tone_builder": make_voice_tone_builder,
    "design_system_codifier": make_design_system_codifier,
}


def build_phase3_graph() -> Graph:
    """Construct the Phase 3 Visual & Expressive Identity graph.

    Wires the module-level topology: three MoodBoardConceptualist variants fan out
    directly into ``converge_decider`` (no intermediate collector node), which then
    fans out into the seven visual specialists.

    Preconditions:
        None — the builder wires the fixed conceptualist variants and specialist
        factories and takes no arguments.
    Postconditions:
        Returns a built ``Graph`` whose entry points are the three moodboard
        conceptualists and whose seven specialist nodes are terminal -- there is
        no compositor; the orchestrator's Phase-3 ``merge_fn``
        (``_merge_phase3_fragments``) assembles their typed ``structured_output``
        fragments into a single ``VisualIdentityOutput`` outside the graph.
    """

    builder = GraphBuilder()

    # ------------------------------------------------------------------
    # 1. Diverge fan-out → converge_decider (direct fan-in, same shape as
    #    Phase 1's five-specialist fan-in into positioning_synthesizer — no
    #    intermediate collector node; Strands' Graph engine assembles every
    #    completed predecessor's output into converge_decider's input once
    #    the whole diverge batch (all three entry-point conceptualists)
    #    finishes).
    # ------------------------------------------------------------------
    converge_node = builder.add_node(make_converge_decider(), node_id="converge_decider")
    wire_fan_out_fan_in(
        builder,
        [
            (f"MoodBoardConceptualist_{variant}", make_moodboard_conceptualist(variant))
            for variant in _PHASE3_CONCEPTUALIST_VARIANTS
        ],
        converge_node,
    )

    # ------------------------------------------------------------------
    # 2. Post-converge specialist fan-out
    # ------------------------------------------------------------------
    fan_out_nodes = [
        builder.add_node(factory(), node_id=node_id)
        for node_id, factory in _PHASE3_SPECIALIST_FACTORIES.items()
    ]
    for node in fan_out_nodes:
        builder.add_edge(converge_node, node)

    return builder.build()
