"""Phase 2 — Narrative & Messaging swarm (creative handoff chain).

Six agents collaborate via dynamic handoffs: Storyteller (entry) hands
off to ArchetypeAnalyst, then TaglineWriter, MessageMapper,
PersonaBuilder, and finally VoicePrinciplesDrafter.  Agents may hand
back upstream when revisions are needed (e.g. archetype mis-alignment).
"""

from __future__ import annotations

from strands.multiagent.swarm import Swarm

from branding_team.agents import (
    make_archetype_analyst,
    make_message_mapper,
    make_persona_builder,
    make_storyteller,
    make_tagline_writer,
    make_voice_principles_drafter,
)


def build_phase2_swarm() -> Swarm:
    """Build the Phase 2 Narrative & Messaging swarm.

    Agents:
        Storyteller (entry), ArchetypeAnalyst, TaglineWriter,
        MessageMapper, PersonaBuilder, VoicePrinciplesDrafter

    The swarm allows up to 10 handoffs and times out after 180 seconds.

    Returns:
        A configured ``Swarm`` instance ready for invocation.
    """
    storyteller = make_storyteller()
    archetype_analyst = make_archetype_analyst()
    tagline_writer = make_tagline_writer()
    message_mapper = make_message_mapper()
    persona_builder = make_persona_builder()
    voice_drafter = make_voice_principles_drafter()

    return Swarm(
        nodes=[
            storyteller,
            archetype_analyst,
            tagline_writer,
            message_mapper,
            persona_builder,
            voice_drafter,
        ],
        entry_point=storyteller,
        max_handoffs=10,
        execution_timeout=180.0,
    )
