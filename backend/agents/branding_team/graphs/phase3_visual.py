"""Phase 3 -- Visual & Expressive Identity (Graph-of-Swarm).

This is the most complex phase in the branding pipeline.  An inner
**Swarm** handles divergent moodboard exploration (CreativeDirector
dispatches three style-variant Conceptualists), then an outer **Graph**
converges the candidates, fans out to seven specialist builders in
parallel, and joins everything in a Visual Identity Compositor.

Pattern:

    [diverge_swarm] --> [converge_decider] --+--> [logo_specifier]
                                             +--> [color_system_builder]
                                             +--> [typography_builder]
                                             +--> [iconography_director]        --> [visual_compositor]
                                             +--> [photography_video_director]
                                             +--> [voice_tone_builder]
                                             +--> [design_system_codifier]
"""

from __future__ import annotations

from strands.multiagent import GraphBuilder, Swarm
from strands.multiagent.graph import Graph

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
from branding_team.graphs.shared import build_agent


def build_phase3_graph() -> Graph:
    """Construct the Phase 3 Visual & Expressive Identity graph.

    Returns a :class:`Graph` whose entry point is the diverge swarm and
    whose terminal node is the ``visual_compositor``.
    """

    # ------------------------------------------------------------------
    # 1. Inner Diverge Swarm
    # ------------------------------------------------------------------
    creative_director = make_creative_director()
    conceptualist_editorial = make_moodboard_conceptualist("Editorial")
    conceptualist_minimalist = make_moodboard_conceptualist("Minimalist")
    conceptualist_bold = make_moodboard_conceptualist("Bold")

    diverge_swarm = Swarm(
        nodes=[
            creative_director,
            conceptualist_editorial,
            conceptualist_minimalist,
            conceptualist_bold,
        ],
        entry_point=creative_director,
        max_handoffs=12,
        execution_timeout=120.0,
    )

    # ------------------------------------------------------------------
    # 2. Outer Graph
    # ------------------------------------------------------------------
    builder = GraphBuilder()

    # Swarm node (entry point)
    diverge_node = builder.add_node(diverge_swarm, node_id="diverge_swarm")

    # Convergence decider
    converge_node = builder.add_node(make_converge_decider(), node_id="converge_decider")

    # Post-converge fan-out specialists (all run in parallel after converge)
    logo_node = builder.add_node(make_logo_specifier(), node_id="logo_specifier")
    color_node = builder.add_node(make_color_system_builder(), node_id="color_system_builder")
    typo_node = builder.add_node(make_typography_builder(), node_id="typography_builder")
    icon_node = builder.add_node(make_iconography_director(), node_id="iconography_director")
    photo_node = builder.add_node(
        make_photography_video_director(), node_id="photography_video_director"
    )
    voice_node = builder.add_node(make_voice_tone_builder(), node_id="voice_tone_builder")
    design_node = builder.add_node(make_design_system_codifier(), node_id="design_system_codifier")

    # Visual compositor (join node) -- inline agent
    compositor = build_agent(
        name="visual_compositor",
        description="Assembles all visual identity fragments into a unified VisualIdentityOutput.",
        system_prompt=(
            "You are a Visual Identity Compositor. Assemble all visual identity fragments into a unified "
            "VisualIdentityOutput. Combine the moodboard candidates from the diverge phase, the creative "
            "refinement decision, logo suite, color palette, typography system, iconography style, "
            "illustration style, photography direction, video direction, motion principles, data "
            "visualization style, digital adaptations, voice tone spectrum, language dos/donts, and "
            "design system. Output comprehensive valid JSON."
        ),
    )
    compositor_node = builder.add_node(compositor, node_id="visual_compositor")

    # ------------------------------------------------------------------
    # 3. Edges
    # ------------------------------------------------------------------
    # diverge_swarm -> converge_decider
    builder.add_edge(diverge_node, converge_node)

    # converge_decider -> each fan-out specialist
    fan_out_nodes = [
        logo_node,
        color_node,
        typo_node,
        icon_node,
        photo_node,
        voice_node,
        design_node,
    ]
    for node in fan_out_nodes:
        builder.add_edge(converge_node, node)

    # each fan-out specialist -> visual_compositor
    for node in fan_out_nodes:
        builder.add_edge(node, compositor_node)

    # ------------------------------------------------------------------
    # 4. Entry point & build
    # ------------------------------------------------------------------
    builder.set_entry_point("diverge_swarm")

    return builder.build()
