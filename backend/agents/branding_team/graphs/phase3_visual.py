"""Phase 3 -- Visual & Expressive Identity (Graph).

Three MoodBoardConceptualist variants fan out in parallel into a
CreativeDirector collector, then an outer converge → specialist fan-out →
compositor sequence finishes the visual identity.

This is intentionally a Graph rather than a Swarm. Agents built with
``structured_output=`` force Strands' structured-output tool and then
stop the agent loop (``stop_loop=True``), so they never call
``handoff_to_agent``. The diverge step therefore uses the same
fan-out/fan-in helper as Phase 1.

Pattern::

    MoodBoardConceptualist_Editorial ──┐
    MoodBoardConceptualist_Minimalist ─┼──▶ CreativeDirector ──▶ converge_decider
    MoodBoardConceptualist_Bold ───────┘         │
                                                 +--> logo_specifier ──────────┐
                                                 +--> color_system_builder ────┤
                                                 +--> typography_builder ──────┤
                                                 +--> iconography_director ────┼──▶ visual_compositor
                                                 +--> photography_video_director┤
                                                 +--> voice_tone_builder ──────┤
                                                 +--> design_system_codifier ──┘
"""

from __future__ import annotations

from strands.multiagent.graph import Graph, GraphBuilder

from branding_team.agents import (
    make_color_system_builder,
    make_converge_decider,
    make_creative_director,
    make_design_system_codifier,
    make_iconography_director,
    make_logo_specifier,
    make_moodboard_conceptualist,
    make_photography_video_director,
    make_typography_builder,
    make_voice_tone_builder,
)
from branding_team.graphs.shared import build_compositor, build_fan_out_fan_in

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

    Returns a :class:`Graph` whose entry points are the three moodboard
    conceptualists and whose terminal node is the ``visual_compositor``.
    """

    builder = GraphBuilder()

    # ------------------------------------------------------------------
    # 1. Diverge fan-out → CreativeDirector collector
    # ------------------------------------------------------------------
    creative_director = builder.add_node(make_creative_director(), node_id="CreativeDirector")
    build_fan_out_fan_in(
        builder,
        [
            (
                f"MoodBoardConceptualist_{variant}",
                # Bind variant in default arg so the lambda closes over the
                # loop value, not the final iteration.
                (lambda v=variant: make_moodboard_conceptualist(v)),
            )
            for variant in _PHASE3_CONCEPTUALIST_VARIANTS
        ],
        creative_director,
    )

    # ------------------------------------------------------------------
    # 2. Converge + post-converge specialist fan-out
    # ------------------------------------------------------------------
    converge_node = builder.add_node(make_converge_decider(), node_id="converge_decider")
    builder.add_edge(creative_director, converge_node)

    fan_out_nodes = [
        builder.add_node(factory(), node_id=node_id)
        for node_id, factory in _PHASE3_SPECIALIST_FACTORIES.items()
    ]
    for node in fan_out_nodes:
        builder.add_edge(converge_node, node)

    # ------------------------------------------------------------------
    # 3. Visual compositor (join node) -- inline agent
    # ------------------------------------------------------------------
    # Join node only: not one of the Phase 3 structured_output factories.
    # Keep the JSON instruction; strip fields no upstream agent produces.
    compositor_agent = build_compositor(
        name="visual_compositor",
        description="Assembles all visual identity fragments into a unified VisualIdentityOutput.",
        system_prompt=(
            "You are a Visual Identity Compositor. Assemble all visual identity fragments into a unified "
            "VisualIdentityOutput. Combine the moodboard candidates from the diverge phase, the creative "
            "refinement decision, logo suite, color palette, typography system, iconography style, "
            "illustration style, photography direction, video direction, motion principles, voice tone "
            "spectrum, language dos/don'ts, and design system. Output comprehensive valid JSON."
        ),
    )
    compositor = builder.add_node(compositor_agent, node_id="visual_compositor")
    for node in fan_out_nodes:
        builder.add_edge(node, compositor)

    return builder.build()
