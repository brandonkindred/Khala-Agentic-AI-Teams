"""Agent factory functions for the branding team Strands SDK pipeline.

Each function returns a configured ``strands.Agent`` instance for use as a
node in a ``GraphBuilder`` graph.  Agents are grouped by phase. Every
``make_*`` factory renders its system prompt from an ``AgentPromptSpec``
via ``render_agent_prompt`` — there are no hand-written prompt-builder
paths in this module.

``BrandComplianceAgent`` is the only non-Strands class; it runs
outside the graph as a post-processing utility.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List

from pydantic import BaseModel
from strands import Agent

from .graphs.shared import build_agent, phase_agent_key
from .models import (
    AUDIENCE_SEGMENTS_MAX,
    AUDIENCE_SEGMENTS_MIN,
    BRAND_ARCHETYPES_MAX,
    BRAND_ARCHETYPES_MIN,
    BRAND_IN_ACTION_MAX,
    BRAND_IN_ACTION_MIN,
    CHANNEL_CONTENT_TYPES_MAX,
    CHANNEL_CONTENT_TYPES_MIN,
    CHANNEL_DONTS_MAX,
    CHANNEL_DONTS_MIN,
    CHANNEL_DOS_MAX,
    CHANNEL_DOS_MIN,
    COLOR_PALETTE_MAX,
    COLOR_PALETTE_MIN,
    CORE_VALUES_MAX,
    CORE_VALUES_MIN,
    DIFFERENTIATION_PILLARS_MAX,
    DIFFERENTIATION_PILLARS_MIN,
    EDITORIAL_QUALITY_BAR_MAX,
    EDITORIAL_QUALITY_BAR_MIN,
    MESSAGING_PILLARS_MAX,
    MESSAGING_PILLARS_MIN,
    PERSONA_PROFILES_MAX,
    PERSONA_PROFILES_MIN,
    STYLE_DONTS_MAX,
    STYLE_DONTS_MIN,
    STYLE_DOS_MAX,
    STYLE_DOS_MIN,
    TYPOGRAPHY_SYSTEM_MAX,
    TYPOGRAPHY_SYSTEM_MIN,
    VOICE_PRINCIPLES_MAX,
    VOICE_PRINCIPLES_MIN,
    ApprovalWorkflowsOutput,
    AssetWikiOutput,
    AudienceSegmentsOutput,
    BrandArchetypesOutput,
    BrandArchitectureOutput,
    BrandCheckRequest,
    BrandCheckResult,
    BrandDiscoveryAudit,
    BrandExperiencePrinciplesOutput,
    BrandGuidelinesOutput,
    BrandHealthKPIsOutput,
    BrandInActionOutput,
    BrandingMission,
    BrandPhase,
    BrandStoryOutput,
    ChannelGuidelineOutput,
    ColorPaletteSystemOutput,
    CoreValuesOutput,
    CreativeRefinementDecision,
    DesignSystemDefinition,
    DifferentiationPillarsOutput,
    EvolutionFrameworkOutput,
    IconographyOutput,
    LogoSuiteOutput,
    MessagingFrameworkOutput,
    MoodBoardConcept,
    OwnershipOutput,
    PersonaProfilesOutput,
    PhotographyVideoOutput,
    PositioningOutput,
    PurposeVisionOutput,
    TaglineOutput,
    TrainingOnboardingOutput,
    TypographySystemOutput,
    VoiceToneOutput,
    WritingGuidelinesOutput,
)
from .prompt_spec import AgentPromptSpec, PromptFieldSpec, render_agent_prompt

# ===================================================================
# Phase 1 — Strategic Core  (Graph: fan-out / fan-in)
# ===================================================================

_PHASE1_AGENT_KEY = phase_agent_key(BrandPhase.STRATEGIC_CORE)


_DISCOVERY_AUDITOR_PROMPT = AgentPromptSpec(
    opening=(
        "You are a Brand Discovery Analyst. Given a branding mission, produce a comprehensive "
        "brand discovery audit."
    ),
    structured_output=BrandDiscoveryAudit,
    closing="Be specific and grounded in the company description and target audience provided.",
)


def make_discovery_auditor() -> Agent:
    """Build the Phase 1 Discovery Auditor agent.

    Postconditions:
        Returns an ``Agent`` named ``discovery_auditor`` whose structured
        output is a ``BrandDiscoveryAudit`` covering current brand
        perception, market position, SWOT, and stakeholder insights. The
        agent is routed through the ``branding_strategic_core`` agent_key
        tier.
    """
    return build_agent(
        name="discovery_auditor",
        description="Analyses current brand perception, SWOT, and stakeholder insights.",
        system_prompt=render_agent_prompt(_DISCOVERY_AUDITOR_PROMPT),
        structured_output=BrandDiscoveryAudit,
        agent_key=_PHASE1_AGENT_KEY,
    )


_PURPOSE_VISION_PROMPT = AgentPromptSpec(
    opening="You are a Purpose & Vision Writer. Given a branding mission, write three things:",
    structured_output=PurposeVisionOutput,
    closing="Be concise, inspiring, and specific to the company.",
)


def make_purpose_vision_writer() -> Agent:
    """Build the Phase 1 Purpose & Vision Writer agent.

    Postconditions:
        Returns an ``Agent`` named ``purpose_vision_writer`` whose
        structured output is a ``PurposeVisionOutput`` containing the
        brand purpose, mission statement, and vision statement. The
        agent is routed through the ``branding_strategic_core`` agent_key
        tier.
    """
    return build_agent(
        name="purpose_vision_writer",
        description="Crafts brand purpose, mission statement, and vision statement.",
        system_prompt=render_agent_prompt(_PURPOSE_VISION_PROMPT),
        structured_output=PurposeVisionOutput,
        agent_key=_PHASE1_AGENT_KEY,
    )


_VALUES_ARTICULATOR_PROMPT = AgentPromptSpec(
    opening=(
        "You are a Values Articulator. Given a branding mission with optional seed values, "
        f"produce a list of {CORE_VALUES_MIN}-{CORE_VALUES_MAX} core values."
    ),
    structured_output=CoreValuesOutput,
)


def make_values_articulator() -> Agent:
    """Build the Phase 1 Values Articulator agent.

    Postconditions:
        Returns an ``Agent`` named ``values_articulator`` whose structured
        output is a ``CoreValuesOutput`` listing core values (count bounded by
        ``CORE_VALUES_MIN``/``CORE_VALUES_MAX``), each with a behavioral
        definition and observable behaviors. The agent is routed through
        the ``branding_strategic_core`` agent_key tier.
    """
    return build_agent(
        name="values_articulator",
        description="Defines core values with behavioral definitions and observable behaviors.",
        system_prompt=render_agent_prompt(_VALUES_ARTICULATOR_PROMPT),
        structured_output=CoreValuesOutput,
        agent_key=_PHASE1_AGENT_KEY,
    )


_AUDIENCE_SEGMENTER_PROMPT = AgentPromptSpec(
    opening=(
        "You are an Audience Segmenter. Given a branding mission, identify "
        f"{AUDIENCE_SEGMENTS_MIN}-{AUDIENCE_SEGMENTS_MAX} target audience segments."
    ),
    structured_output=AudienceSegmentsOutput,
    closing="Ground your analysis in the company description and stated target audience.",
)


def make_audience_segmenter() -> Agent:
    """Build the Phase 1 Audience Segmenter agent.

    Postconditions:
        Returns an ``Agent`` named ``audience_segmenter`` whose structured
        output is an ``AudienceSegmentsOutput`` describing target audience
        segments (count bounded by ``AUDIENCE_SEGMENTS_MIN``/``AUDIENCE_SEGMENTS_MAX``)
        with pain points, goals, and decision drivers. The agent is
        routed through the ``branding_strategic_core`` agent_key tier.
    """
    return build_agent(
        name="audience_segmenter",
        description="Segments target audience with psychographic depth.",
        system_prompt=render_agent_prompt(_AUDIENCE_SEGMENTER_PROMPT),
        structured_output=AudienceSegmentsOutput,
        agent_key=_PHASE1_AGENT_KEY,
    )


_DIFFERENTIATION_MAPPER_PROMPT = AgentPromptSpec(
    opening=(
        "You are a Differentiation Mapper. Given a branding mission with optional "
        f"differentiators, produce {DIFFERENTIATION_PILLARS_MIN}-{DIFFERENTIATION_PILLARS_MAX} "
        "differentiation pillars."
    ),
    structured_output=DifferentiationPillarsOutput,
)


def make_differentiation_mapper() -> Agent:
    """Build the Phase 1 Differentiation Mapper agent.

    Postconditions:
        Returns an ``Agent`` named ``differentiation_mapper`` whose
        structured output is a ``DifferentiationPillarsOutput`` listing
        differentiation pillars (count bounded by ``DIFFERENTIATION_PILLARS_MIN``/
        ``DIFFERENTIATION_PILLARS_MAX``) with proof points and competitive
        context. The agent is routed through the ``branding_strategic_core``
        agent_key tier.
    """
    return build_agent(
        name="differentiation_mapper",
        description="Maps competitive differentiation pillars with proof points.",
        system_prompt=render_agent_prompt(_DIFFERENTIATION_MAPPER_PROMPT),
        structured_output=DifferentiationPillarsOutput,
        agent_key=_PHASE1_AGENT_KEY,
    )


_POSITIONING_SYNTHESIZER_PROMPT = AgentPromptSpec(
    opening=(
        "You are a Positioning Synthesizer. You receive outputs from the discovery auditor, "
        "purpose/vision writer, values articulator, audience segmenter, and differentiation "
        "mapper. Synthesise them into:"
    ),
    structured_output=PositioningOutput,
)


def make_positioning_synthesizer() -> Agent:
    """Build the Phase 1 Positioning Synthesizer agent.

    Postconditions:
        Returns an ``Agent`` named ``positioning_synthesizer`` whose
        structured output is a ``PositioningOutput`` synthesizing the
        other Phase 1 fragments into a positioning statement and brand
        promise. The agent is routed through the ``branding_strategic_core``
        agent_key tier.
    """
    return build_agent(
        name="positioning_synthesizer",
        description="Synthesises all Phase 1 fragments into positioning statement and brand promise.",
        system_prompt=render_agent_prompt(_POSITIONING_SYNTHESIZER_PROMPT),
        structured_output=PositioningOutput,
        agent_key=_PHASE1_AGENT_KEY,
    )


# ===================================================================
# Phase 2 — Narrative & Messaging  (Graph: sequential specialists)
# ===================================================================

_PHASE2_AGENT_KEY = phase_agent_key(BrandPhase.NARRATIVE_MESSAGING)


_STORYTELLER_PROMPT = AgentPromptSpec(
    opening="You are a Brand Storyteller. Using the strategic core output and branding mission, craft:",
    structured_output=BrandStoryOutput,
)


def make_storyteller() -> Agent:
    """Build the Phase 2 Storyteller agent.

    Postconditions:
        Returns an ``Agent`` named ``Storyteller`` whose structured output
        is a ``BrandStoryOutput`` containing the brand story, hero
        narrative, and boilerplate variants. The agent is routed through
        the ``branding_narrative_messaging`` agent_key tier.
    """
    return build_agent(
        name="Storyteller",
        description="Crafts the brand story, hero narrative, and boilerplate variants.",
        system_prompt=render_agent_prompt(_STORYTELLER_PROMPT),
        structured_output=BrandStoryOutput,
        agent_key=_PHASE2_AGENT_KEY,
    )


_ARCHETYPE_ANALYST_PROMPT = AgentPromptSpec(
    opening=(
        "You are a Brand Archetype Analyst. Using the brand story from Inputs from previous "
        f"nodes and the strategic core as read-only context, select {BRAND_ARCHETYPES_MIN}-"
        f"{BRAND_ARCHETYPES_MAX} brand archetypes (e.g. The Sage, The Creator, The Explorer) "
        "that fit the narrative, and add:"
    ),
    fields=(
        PromptFieldSpec(
            "brand_archetypes",
            "for each archetype: archetype (name), rationale (why this fits), and "
            "personality_traits (3-5 traits)",
        ),
    ),
)


def make_archetype_analyst() -> Agent:
    """Build the Phase 2 Archetype Analyst agent.

    Postconditions:
        Returns an ``Agent`` named ``ArchetypeAnalyst`` whose structured
        output is a ``BrandArchetypesOutput`` selecting brand archetypes
        (count bounded by ``BRAND_ARCHETYPES_MIN``/``BRAND_ARCHETYPES_MAX``)
        with rationale and personality traits — its own field only, the
        Storyteller fragment is merged in separately by the orchestrator.
        The agent is routed through the ``branding_narrative_messaging``
        agent_key tier.
    """
    return build_agent(
        name="ArchetypeAnalyst",
        description="Selects brand archetypes with rationale and personality traits.",
        system_prompt=render_agent_prompt(_ARCHETYPE_ANALYST_PROMPT),
        structured_output=BrandArchetypesOutput,
        agent_key=_PHASE2_AGENT_KEY,
    )


_TAGLINE_WRITER_PROMPT = AgentPromptSpec(
    opening=(
        "You are a Tagline Writer. Using the brand story and archetypes from Inputs from "
        "previous nodes and the strategic core as read-only context, add:"
    ),
    fields=(
        PromptFieldSpec("tagline", "a memorable brand tagline (max 8 words)"),
        PromptFieldSpec("tagline_rationale", "why this tagline works"),
        PromptFieldSpec(
            "elevator_pitches",
            "three variants: tier '5-second' pitch, tier '30-second' pitch, and tier "
            "'2-minute' pitch",
        ),
    ),
)


def make_tagline_writer() -> Agent:
    """Build the Phase 2 Tagline Writer agent.

    Postconditions:
        Returns an ``Agent`` named ``TaglineWriter`` whose structured
        output is a ``TaglineOutput`` containing a tagline, tagline
        rationale, and elevator pitches — its own fields only, merged
        with the other five specialists' fragments by the orchestrator.
        The agent is routed through the ``branding_narrative_messaging``
        agent_key tier.
    """
    return build_agent(
        name="TaglineWriter",
        description="Creates tagline, tagline rationale, and elevator pitches.",
        system_prompt=render_agent_prompt(_TAGLINE_WRITER_PROMPT),
        structured_output=TaglineOutput,
        agent_key=_PHASE2_AGENT_KEY,
    )


_MESSAGE_MAPPER_PROMPT = AgentPromptSpec(
    opening=(
        "You are a Message Mapper. Using all prior narrative fields from Inputs from previous "
        "nodes as read-only context, add:"
    ),
    fields=(
        PromptFieldSpec(
            "messaging_framework",
            f"{MESSAGING_PILLARS_MIN}-{MESSAGING_PILLARS_MAX} messaging pillars, each with: "
            "pillar, key_message, and proof_points",
        ),
        PromptFieldSpec(
            "audience_message_maps",
            "one per audience segment, each with: audience_segment, primary_message, "
            "supporting_messages, and tone_adjustments",
        ),
    ),
)


def make_message_mapper() -> Agent:
    """Build the Phase 2 Message Mapper agent.

    Postconditions:
        Returns an ``Agent`` named ``MessageMapper`` whose structured
        output is a ``MessagingFrameworkOutput`` containing a messaging
        framework and per-segment audience message maps — its own fields
        only, merged with the other five specialists' fragments by the
        orchestrator. The agent is routed through the
        ``branding_narrative_messaging`` agent_key tier.
    """
    return build_agent(
        name="MessageMapper",
        description="Builds messaging framework pillars and audience message maps.",
        system_prompt=render_agent_prompt(_MESSAGE_MAPPER_PROMPT),
        structured_output=MessagingFrameworkOutput,
        agent_key=_PHASE2_AGENT_KEY,
    )


_PERSONA_BUILDER_PROMPT = AgentPromptSpec(
    opening=(
        "You are a Persona Builder. Using audience segments and all prior narrative fields "
        "from Inputs from previous nodes as read-only context, create:"
    ),
    fields=(
        PromptFieldSpec(
            "persona_profiles",
            f"{PERSONA_PROFILES_MIN}-{PERSONA_PROFILES_MAX} persona profiles, each with: name, "
            "role, demographics, psychographics, "
            "goals, frustrations, media_habits, jobs_to_be_done",
        ),
    ),
)


def make_persona_builder() -> Agent:
    """Build the Phase 2 Persona Builder agent.

    Postconditions:
        Returns an ``Agent`` named ``PersonaBuilder`` whose structured
        output is a ``PersonaProfilesOutput`` containing persona profiles
        (count bounded by ``PERSONA_PROFILES_MIN``/``PERSONA_PROFILES_MAX``)
        — its own field only, merged with the other five specialists'
        fragments by the orchestrator. The agent is routed through the
        ``branding_narrative_messaging`` agent_key tier.
    """
    return build_agent(
        name="PersonaBuilder",
        description="Creates rich persona profiles with psychographic depth.",
        system_prompt=render_agent_prompt(_PERSONA_BUILDER_PROMPT),
        structured_output=PersonaProfilesOutput,
        agent_key=_PHASE2_AGENT_KEY,
    )


_VOICE_PRINCIPLES_DRAFTER_PROMPT = AgentPromptSpec(
    opening=(
        "You are a Voice Principles Drafter. Using all prior narrative fields from Inputs from "
        "previous nodes and the mission's desired_voice as read-only context, produce "
        "writing_guidelines:"
    ),
    fields=(
        PromptFieldSpec(
            "voice_principles",
            f"{VOICE_PRINCIPLES_MIN}-{VOICE_PRINCIPLES_MAX} principles "
            "(e.g. 'Use a confident, human voice')",
        ),
        PromptFieldSpec("style_dos", f"{STYLE_DOS_MIN}-{STYLE_DOS_MAX} writing best practices"),
        PromptFieldSpec("style_donts", f"{STYLE_DONTS_MIN}-{STYLE_DONTS_MAX} things to avoid"),
        PromptFieldSpec(
            "editorial_quality_bar",
            f"{EDITORIAL_QUALITY_BAR_MIN}-{EDITORIAL_QUALITY_BAR_MAX} quality standards every "
            "piece must meet",
        ),
    ),
    closing="\nThis is the final step in narrative development.",
)


def make_voice_principles_drafter() -> Agent:
    """Build the Phase 2 Voice Principles Drafter agent.

    Postconditions:
        Returns an ``Agent`` named ``VoicePrinciplesDrafter`` whose
        structured output is a ``WritingGuidelinesOutput`` containing voice
        principles, style dos/don'ts, and an editorial quality bar — its own
        field only, merged with the other five specialists' fragments by the
        orchestrator. The final step in narrative development. The agent is
        routed through the ``branding_narrative_messaging`` agent_key tier.
    """
    return build_agent(
        name="VoicePrinciplesDrafter",
        description="Defines writing guidelines: voice principles, style dos/don'ts, editorial bar.",
        system_prompt=render_agent_prompt(_VOICE_PRINCIPLES_DRAFTER_PROMPT),
        structured_output=WritingGuidelinesOutput,
        agent_key=_PHASE2_AGENT_KEY,
    )


# ===================================================================
# Phase 3 — Visual & Expressive Identity  (Graph: diverge fan-out + converge fan-out)
# ===================================================================

_PHASE3_AGENT_KEY = phase_agent_key(BrandPhase.VISUAL_IDENTITY)

# --- Diverge fan-out agents ---


def _moodboard_conceptualist_prompt(variant: str) -> AgentPromptSpec:
    """Build the MoodBoard Conceptualist prompt spec for one visual-direction variant.

    Preconditions:
        ``variant`` is a non-empty string.
    Postconditions:
        Returns an ``AgentPromptSpec`` whose opening interpolates
        ``variant.lower()`` and whose fields name the five
        ``MoodBoardConcept`` attributes.
    """
    assert isinstance(variant, str) and variant.strip(), "variant must be a non-empty string"
    return AgentPromptSpec(
        opening=(
            f"You are a MoodBoard Conceptualist specialising in {variant.lower()} visual "
            f"directions. Given a brand's strategic core and narrative, create a moodboard "
            f"concept with:"
        ),
        structured_output=MoodBoardConcept,
    )


def make_moodboard_conceptualist(variant: str) -> Agent:
    """Build a Phase 3 MoodBoard Conceptualist agent for one visual direction.

    Preconditions:
        ``variant`` is a non-empty string naming a visual direction
        (e.g. ``"Minimalist"``).

    Postconditions:
        Returns an ``Agent`` named ``MoodBoardConceptualist_{variant}``
        whose prompt is specialized to ``variant`` and whose output is a
        moodboard concept (title, visual direction, color story,
        typography direction, image style) that fans directly into
        ``converge_decider`` in the Phase 3 Graph. The agent is routed
        through the ``branding_visual_identity`` agent_key tier.
    """
    assert isinstance(variant, str) and variant.strip(), "variant must be a non-empty string"
    return build_agent(
        name=f"MoodBoardConceptualist_{variant}",
        description=f"Generates a {variant.lower()} visual direction moodboard concept.",
        system_prompt=render_agent_prompt(_moodboard_conceptualist_prompt(variant)),
        structured_output=MoodBoardConcept,
        agent_key=_PHASE3_AGENT_KEY,
    )


# --- Post-diverge Graph nodes ---


_CONVERGE_DECIDER_PROMPT = AgentPromptSpec(
    opening=(
        "You are a Creative Convergence Decider. You receive moodboard candidates from the "
        "diverge phase plus the brand's strategic core and values. Score each candidate on:\n"
        "- Audience resonance\n"
        "- Distinctiveness vs competitors\n"
        "- Cross-channel consistency\n"
        "- Execution feasibility\n\n"
        "Produce:"
    ),
    structured_output=CreativeRefinementDecision,
)


def make_converge_decider() -> Agent:
    """Build the Phase 3 Converge Decider agent.

    Postconditions:
        Returns an ``Agent`` named ``converge_decider`` that scores the
        diverge-phase moodboard candidates against audience resonance,
        distinctiveness, cross-channel consistency, and feasibility, and
        selects a winning candidate. The agent is routed through the
        ``branding_visual_identity`` agent_key tier.
    """
    return build_agent(
        name="converge_decider",
        description="Scores moodboard candidates and selects a winner.",
        system_prompt=render_agent_prompt(_CONVERGE_DECIDER_PROMPT),
        structured_output=CreativeRefinementDecision,
        agent_key=_PHASE3_AGENT_KEY,
    )


_LOGO_SPECIFIER_PROMPT = AgentPromptSpec(
    opening=(
        "You are a Logo Specifier. Based on the winning moodboard direction, define a logo "
        "suite. For each variant (primary, monochrome, icon-only, reversed), specify:"
    ),
    structured_output=LogoSuiteOutput,
)


def make_logo_specifier() -> Agent:
    """Build the Phase 3 Logo Specifier agent.

    Postconditions:
        Returns an ``Agent`` named ``logo_specifier`` whose output
        defines the logo suite (primary, monochrome, icon-only,
        reversed variants) with usage rules for the winning moodboard
        direction. The agent is routed through the
        ``branding_visual_identity`` agent_key tier.
    """
    return build_agent(
        name="logo_specifier",
        description="Defines logo suite with usage rules.",
        system_prompt=render_agent_prompt(_LOGO_SPECIFIER_PROMPT),
        structured_output=LogoSuiteOutput,
        agent_key=_PHASE3_AGENT_KEY,
    )


_COLOR_SYSTEM_BUILDER_PROMPT = AgentPromptSpec(
    opening=(
        "You are a Color System Builder. Based on the winning moodboard direction, define "
        f"{COLOR_PALETTE_MIN}-{COLOR_PALETTE_MAX} colors. Include primary, secondary, accent, "
        "surface, and critical colors."
    ),
    structured_output=ColorPaletteSystemOutput,
)


def make_color_system_builder() -> Agent:
    """Build the Phase 3 Color System Builder agent.

    Postconditions:
        Returns an ``Agent`` named ``color_system_builder`` whose output
        defines brand colors (count bounded by ``COLOR_PALETTE_MIN``/
        ``COLOR_PALETTE_MAX``) with hex values, usage, and psychological
        rationale for the winning moodboard direction. The agent is
        routed through the ``branding_visual_identity`` agent_key tier.
    """
    return build_agent(
        name="color_system_builder",
        description="Builds the brand color palette with psychological rationale.",
        system_prompt=render_agent_prompt(_COLOR_SYSTEM_BUILDER_PROMPT),
        structured_output=ColorPaletteSystemOutput,
        agent_key=_PHASE3_AGENT_KEY,
    )


_TYPOGRAPHY_BUILDER_PROMPT = AgentPromptSpec(
    opening=(
        "You are a Typography Builder. Based on the winning moodboard direction, define a "
        f"typography system with {TYPOGRAPHY_SYSTEM_MIN}-{TYPOGRAPHY_SYSTEM_MAX} type roles "
        "(display, body, caption, code). For each:"
    ),
    structured_output=TypographySystemOutput,
)


def make_typography_builder() -> Agent:
    """Build the Phase 3 Typography Builder agent.

    Postconditions:
        Returns an ``Agent`` named ``typography_builder`` whose output
        defines a typography system for the winning moodboard direction; the
        number of type roles is governed by ``TYPOGRAPHY_SYSTEM_MIN`` and
        ``TYPOGRAPHY_SYSTEM_MAX``. The agent is routed through the
        ``branding_visual_identity`` agent_key tier.
    """
    return build_agent(
        name="typography_builder",
        description="Defines the typography system.",
        system_prompt=render_agent_prompt(_TYPOGRAPHY_BUILDER_PROMPT),
        structured_output=TypographySystemOutput,
        agent_key=_PHASE3_AGENT_KEY,
    )


_ICONOGRAPHY_PROMPT = AgentPromptSpec(
    opening="You are an Iconography Director. Based on the winning moodboard, define:",
    structured_output=IconographyOutput,
)


def make_iconography_director() -> Agent:
    """Build the Phase 3 Iconography Director agent.

    Postconditions:
        Returns an ``Agent`` named ``iconography_director`` whose output
        defines the iconography and illustration style for the winning
        moodboard direction. The agent is routed through the
        ``branding_visual_identity`` agent_key tier.
    """
    return build_agent(
        name="iconography_director",
        description="Defines iconography and illustration style.",
        system_prompt=render_agent_prompt(_ICONOGRAPHY_PROMPT),
        structured_output=IconographyOutput,
        agent_key=_PHASE3_AGENT_KEY,
    )


_PHOTOGRAPHY_VIDEO_DIRECTOR_PROMPT = AgentPromptSpec(
    opening="You are a Photography & Video Director. Based on the winning moodboard, define:",
    structured_output=PhotographyVideoOutput,
)


def make_photography_video_director() -> Agent:
    """Build the Phase 3 Photography & Video Director agent.

    Postconditions:
        Returns an ``Agent`` named ``photography_video_director`` whose
        output defines photography direction, video direction, and
        motion principles for the winning moodboard direction. The agent
        is routed through the ``branding_visual_identity`` agent_key
        tier.
    """
    return build_agent(
        name="photography_video_director",
        description="Defines photography direction, video direction, and motion principles.",
        system_prompt=render_agent_prompt(_PHOTOGRAPHY_VIDEO_DIRECTOR_PROMPT),
        structured_output=PhotographyVideoOutput,
        agent_key=_PHASE3_AGENT_KEY,
    )


_VOICE_TONE_BUILDER_PROMPT = AgentPromptSpec(
    opening=(
        "You are a Voice & Tone Builder. Using the brand narrative's writing guidelines and "
        "the moodboard direction, define:"
    ),
    structured_output=VoiceToneOutput,
)


def make_voice_tone_builder() -> Agent:
    """Build the Phase 3 Voice & Tone Builder agent.

    Postconditions:
        Returns an ``Agent`` named ``voice_tone_builder`` whose output
        defines the voice/tone spectrum and language dos/don'ts, drawing
        on the brand narrative's writing guidelines and the moodboard
        direction. The agent is routed through the
        ``branding_visual_identity`` agent_key tier.
    """
    return build_agent(
        name="voice_tone_builder",
        description="Defines voice/tone spectrum and language dos/don'ts.",
        system_prompt=render_agent_prompt(_VOICE_TONE_BUILDER_PROMPT),
        structured_output=VoiceToneOutput,
        agent_key=_PHASE3_AGENT_KEY,
    )


_DESIGN_SYSTEM_CODIFIER_PROMPT = AgentPromptSpec(
    opening="You are a Design System Codifier. Based on the full visual identity work, produce:",
    structured_output=DesignSystemDefinition,
)


def make_design_system_codifier() -> Agent:
    """Build the Phase 3 Design System Codifier agent.

    Postconditions:
        Returns an ``Agent`` named ``design_system_codifier`` whose
        output codifies design principles, foundation tokens, and
        component standards from the full visual identity work. The
        agent is routed through the ``branding_visual_identity``
        agent_key tier.
    """
    return build_agent(
        name="design_system_codifier",
        description="Codifies the design system: principles, tokens, component standards.",
        system_prompt=render_agent_prompt(_DESIGN_SYSTEM_CODIFIER_PROMPT),
        structured_output=DesignSystemDefinition,
        agent_key=_PHASE3_AGENT_KEY,
    )


# ===================================================================
# Phase 4 — Experience & Channel Activation  (Graph: fan-out / fan-in)
# ===================================================================

_PHASE4_AGENT_KEY = phase_agent_key(BrandPhase.CHANNEL_ACTIVATION)


_BRAND_EXPERIENCE_PRINCIPLER_PROMPT = AgentPromptSpec(
    opening="You are a Brand Experience Architect. Define:",
    structured_output=BrandExperiencePrinciplesOutput,
)


def make_brand_experience_principler() -> Agent:
    """Build the Phase 4 Brand Experience Principler agent.

    Postconditions:
        Returns an ``Agent`` named ``brand_experience_principler`` whose
        output defines brand experience principles, signature customer
        journey moments, and sensory elements. The agent is routed
        through the ``branding_channel_activation`` agent_key tier.
    """
    return build_agent(
        name="brand_experience_principler",
        description="Defines brand experience principles, signature moments, and sensory elements.",
        system_prompt=render_agent_prompt(_BRAND_EXPERIENCE_PRINCIPLER_PROMPT),
        structured_output=BrandExperiencePrinciplesOutput,
        agent_key=_PHASE4_AGENT_KEY,
    )


def _channel_guide_prompt(channel: str, description: str) -> AgentPromptSpec:
    """Build the channel-guide prompt spec for one activation channel.

    Preconditions:
        ``channel`` and ``description`` are non-empty strings.
    Postconditions:
        Returns an ``AgentPromptSpec`` whose opening interpolates
        ``channel.title()`` / ``channel``, whose fields name the six
        ``ChannelGuidelineOutput`` attributes (``channel``'s description
        is the quoted identifier), and whose closing is
        ``f"Context: {description}"``.
    """
    assert isinstance(channel, str) and channel.strip(), "channel must be a non-empty string"
    assert isinstance(description, str) and description.strip(), (
        "description must be a non-empty string"
    )
    return AgentPromptSpec(
        opening=(
            f"You are a {channel.title()} Channel Specialist. Define guidelines for the "
            f"{channel} channel:"
        ),
        fields=(
            PromptFieldSpec("channel", f"'{channel}'"),
            PromptFieldSpec("strategy", "overall approach for this channel"),
            PromptFieldSpec("dos", f"{CHANNEL_DOS_MIN}-{CHANNEL_DOS_MAX} best practices"),
            PromptFieldSpec("donts", f"{CHANNEL_DONTS_MIN}-{CHANNEL_DONTS_MAX} things to avoid"),
            PromptFieldSpec(
                "content_types",
                f"{CHANNEL_CONTENT_TYPES_MIN}-{CHANNEL_CONTENT_TYPES_MAX} recommended content formats",
            ),
            PromptFieldSpec("frequency_guidance", "recommended cadence"),
        ),
        closing=f"Context: {description}",
    )


def _make_channel_guide(
    channel: str, description: str, structured_output: type[BaseModel]
) -> Agent:
    """Build a channel-specific brand guidelines agent.

    Preconditions:
        ``channel`` and ``description`` are non-empty strings; ``channel``
        is a lowercase identifier suitable for use in an agent name
        (e.g. ``"website"``). ``structured_output`` is the Pydantic model
        for this channel's guideline output (currently always
        ``ChannelGuidelineOutput``, passed explicitly by each call site).

    Postconditions:
        Returns an ``Agent`` named ``f"{channel}_guide"`` whose prompt
        embeds ``channel`` and ``description`` and whose structured
        output defines that channel's strategy, dos/don'ts, content
        types, and cadence per ``structured_output``. The agent is
        routed through the ``branding_channel_activation`` agent_key
        tier.
    """
    if not isinstance(channel, str) or not channel.strip():
        raise ValueError("channel must be a non-empty string")
    if not re.fullmatch(r"[a-z][a-z0-9_]*", channel):
        raise ValueError("channel must be a lowercase identifier (e.g. 'website')")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("description must be a non-empty string")
    if not (isinstance(structured_output, type) and issubclass(structured_output, BaseModel)):
        raise ValueError("structured_output must be a Pydantic BaseModel subclass")
    return build_agent(
        name=f"{channel}_guide",
        description=f"Defines brand guidelines for the {channel} channel.",
        system_prompt=render_agent_prompt(_channel_guide_prompt(channel, description)),
        structured_output=structured_output,
        agent_key=_PHASE4_AGENT_KEY,
    )


def make_website_guide() -> Agent:
    """Build the Phase 4 website channel guide agent.

    Postconditions:
        Returns ``_make_channel_guide("website", ...)`` — see that
        function's contract.
    """
    return _make_channel_guide(
        "website", "Company website, landing pages, product pages.", ChannelGuidelineOutput
    )


def make_social_guide() -> Agent:
    """Build the Phase 4 social media channel guide agent.

    Postconditions:
        Returns ``_make_channel_guide("social", ...)`` — see that
        function's contract.
    """
    return _make_channel_guide(
        "social", "Social media platforms (LinkedIn, Twitter, Instagram).", ChannelGuidelineOutput
    )


def make_email_guide() -> Agent:
    """Build the Phase 4 email channel guide agent.

    Postconditions:
        Returns ``_make_channel_guide("email", ...)`` — see that
        function's contract.
    """
    return _make_channel_guide(
        "email", "Email marketing, newsletters, transactional emails.", ChannelGuidelineOutput
    )


def make_events_guide() -> Agent:
    """Build the Phase 4 events channel guide agent.

    Postconditions:
        Returns ``_make_channel_guide("events", ...)`` — see that
        function's contract.
    """
    return _make_channel_guide(
        "events", "Conferences, webinars, meetups, trade shows.", ChannelGuidelineOutput
    )


def make_partnerships_guide() -> Agent:
    """Build the Phase 4 partnerships channel guide agent.

    Postconditions:
        Returns ``_make_channel_guide("partnerships", ...)`` — see that
        function's contract.
    """
    return _make_channel_guide(
        "partnerships", "Co-branding, sponsorships, partner marketing.", ChannelGuidelineOutput
    )


def make_internal_guide() -> Agent:
    """Build the Phase 4 internal comms channel guide agent.

    Postconditions:
        Returns ``_make_channel_guide("internal", ...)`` — see that
        function's contract.
    """
    return _make_channel_guide(
        "internal", "Internal comms, employee branding, onboarding.", ChannelGuidelineOutput
    )


_BRAND_ARCHITECTURE_BUILDER_PROMPT = AgentPromptSpec(
    opening="You are a Brand Architecture Specialist. Define:",
    structured_output=BrandArchitectureOutput,
)


def make_brand_architecture_builder() -> Agent:
    """Build the Phase 4 Brand Architecture Specialist agent.

    Postconditions:
        Returns an ``Agent`` named ``brand_architecture_builder`` whose
        output defines brand architecture rules, naming conventions,
        and a terminology glossary. The agent is routed through the
        ``branding_channel_activation`` agent_key tier.
    """
    return build_agent(
        name="brand_architecture_builder",
        description="Defines brand architecture rules, naming conventions, and terminology.",
        system_prompt=render_agent_prompt(_BRAND_ARCHITECTURE_BUILDER_PROMPT),
        structured_output=BrandArchitectureOutput,
        agent_key=_PHASE4_AGENT_KEY,
    )


_BRAND_IN_ACTION_ILLUSTRATOR_PROMPT = AgentPromptSpec(
    opening=(
        f"You are a Brand-in-Action Illustrator. Create {BRAND_IN_ACTION_MIN}-{BRAND_IN_ACTION_MAX} "
        "applied examples showing correct vs incorrect brand usage:"
    ),
    structured_output=BrandInActionOutput,
)


def make_brand_in_action_illustrator() -> Agent:
    """Build the Phase 4 Brand-in-Action Illustrator agent.

    Postconditions:
        Returns an ``Agent`` named ``brand_in_action_illustrator`` whose
        output produces correct-vs-incorrect brand usage examples (count
        bounded by ``BRAND_IN_ACTION_MIN``/``BRAND_IN_ACTION_MAX``). The
        agent is routed through the ``branding_channel_activation``
        agent_key tier.
    """
    return build_agent(
        name="brand_in_action_illustrator",
        description="Creates brand-in-action do/don't examples.",
        system_prompt=render_agent_prompt(_BRAND_IN_ACTION_ILLUSTRATOR_PROMPT),
        structured_output=BrandInActionOutput,
        agent_key=_PHASE4_AGENT_KEY,
    )


# ===================================================================
# Phase 5 — Governance & Evolution  (Graph: fan-out / fan-in)
# ===================================================================

_PHASE5_AGENT_KEY = phase_agent_key(BrandPhase.GOVERNANCE)


_OWNERSHIP_DEFINER_PROMPT = AgentPromptSpec(
    opening="You are a Brand Ownership Definer. Define:",
    structured_output=OwnershipOutput,
)


def make_ownership_definer() -> Agent:
    """Build the Phase 5 Brand Ownership Definer agent.

    Postconditions:
        Returns an ``Agent`` named ``ownership_definer`` whose output
        defines the brand ownership model and a decision authority
        matrix. The agent is routed through the ``branding_governance``
        agent_key tier.
    """
    return build_agent(
        name="ownership_definer",
        description="Defines brand ownership model and decision authority matrix.",
        system_prompt=render_agent_prompt(_OWNERSHIP_DEFINER_PROMPT),
        structured_output=OwnershipOutput,
        agent_key=_PHASE5_AGENT_KEY,
    )


_APPROVAL_WORKFLOW_DESIGNER_PROMPT = AgentPromptSpec(
    opening="You are an Approval Workflow Designer. Define:",
    structured_output=ApprovalWorkflowsOutput,
)


def make_approval_workflow_designer() -> Agent:
    """Build the Phase 5 Approval Workflow Designer agent.

    Postconditions:
        Returns an ``Agent`` named ``approval_workflow_designer`` whose
        output defines approval workflows and agency briefing
        protocols. The agent is routed through the
        ``branding_governance`` agent_key tier.
    """
    return build_agent(
        name="approval_workflow_designer",
        description="Designs approval workflows and agency briefing protocols.",
        system_prompt=render_agent_prompt(_APPROVAL_WORKFLOW_DESIGNER_PROMPT),
        structured_output=ApprovalWorkflowsOutput,
        agent_key=_PHASE5_AGENT_KEY,
    )


_ASSET_WIKI_PLANNER_PROMPT = AgentPromptSpec(
    opening="You are an Asset & Wiki Planner. Define:",
    structured_output=AssetWikiOutput,
)


def make_asset_wiki_planner() -> Agent:
    """Build the Phase 5 Asset & Wiki Planner agent.

    Postconditions:
        Returns an ``Agent`` named ``asset_wiki_planner`` whose output
        defines asset management guidance and the brand wiki backlog.
        The agent is routed through the ``branding_governance``
        agent_key tier.
    """
    return build_agent(
        name="asset_wiki_planner",
        description="Plans asset management and brand wiki backlog.",
        system_prompt=render_agent_prompt(_ASSET_WIKI_PLANNER_PROMPT),
        structured_output=AssetWikiOutput,
        agent_key=_PHASE5_AGENT_KEY,
    )


_TRAINING_PLANNER_PROMPT = AgentPromptSpec(
    opening="You are a Training Planner. Define:",
    structured_output=TrainingOnboardingOutput,
)


def make_training_planner() -> Agent:
    """Build the Phase 5 Training Planner agent.

    Postconditions:
        Returns an ``Agent`` named ``training_planner`` whose output
        defines the brand training and onboarding plan. The agent is
        routed through the ``branding_governance`` agent_key tier.
    """
    return build_agent(
        name="training_planner",
        description="Plans brand training and onboarding programmes.",
        system_prompt=render_agent_prompt(_TRAINING_PLANNER_PROMPT),
        structured_output=TrainingOnboardingOutput,
        agent_key=_PHASE5_AGENT_KEY,
    )


_KPI_DESIGNER_PROMPT = AgentPromptSpec(
    opening="You are a Brand KPI Designer. Define:",
    structured_output=BrandHealthKPIsOutput,
)


def make_kpi_designer() -> Agent:
    """Build the Phase 5 Brand KPI Designer agent.

    Postconditions:
        Returns an ``Agent`` named ``kpi_designer`` whose output defines
        brand health KPIs, tracking methodology, and review trigger
        points. The agent is routed through the ``branding_governance``
        agent_key tier.
    """
    return build_agent(
        name="kpi_designer",
        description="Designs brand health KPIs with tracking methodology.",
        system_prompt=render_agent_prompt(_KPI_DESIGNER_PROMPT),
        structured_output=BrandHealthKPIsOutput,
        agent_key=_PHASE5_AGENT_KEY,
    )


_EVOLUTION_FRAMER_PROMPT = AgentPromptSpec(
    opening="You are a Brand Evolution Framer. Define:",
    structured_output=EvolutionFrameworkOutput,
)


def make_evolution_framer() -> Agent:
    """Build the Phase 5 Brand Evolution Framer agent.

    Postconditions:
        Returns an ``Agent`` named ``evolution_framer`` whose output
        defines the brand evolution framework and version control
        cadence. The agent is routed through the ``branding_governance``
        agent_key tier.
    """
    return build_agent(
        name="evolution_framer",
        description="Defines the brand evolution framework and version control cadence.",
        system_prompt=render_agent_prompt(_EVOLUTION_FRAMER_PROMPT),
        structured_output=EvolutionFrameworkOutput,
        agent_key=_PHASE5_AGENT_KEY,
    )


_BRAND_RULES_CODIFIER_PROMPT = AgentPromptSpec(
    opening=(
        "You are a Brand Rules Codifier. Using the full brand context (positioning, promise, "
        "values, narrative, visual identity), produce:"
    ),
    structured_output=BrandGuidelinesOutput,
    closing=(
        "Cover: identity usage, messaging hierarchy, approval gates, asset management, and "
        "evolution."
    ),
)


def make_brand_rules_codifier() -> Agent:
    """Build the Phase 5 Brand Rules Codifier agent.

    Postconditions:
        Returns an ``Agent`` named ``brand_rules_codifier`` whose output
        codifies top-level brand governance rules (count bounded by
        ``BRAND_GUIDELINES_MIN``/``BRAND_GUIDELINES_MAX``). The agent is
        routed through the ``branding_governance`` agent_key tier.
    """
    return build_agent(
        name="brand_rules_codifier",
        description="Codifies top-level brand governance rules.",
        system_prompt=render_agent_prompt(_BRAND_RULES_CODIFIER_PROMPT),
        structured_output=BrandGuidelinesOutput,
        agent_key=_PHASE5_AGENT_KEY,
    )


# ===================================================================
# Brand Compliance (outside the graph — post-processing utility)
# ===================================================================


# Fixed copy reused for every off-brand asset — hoisted out of the per-check
# loop so it is allocated once, not rebuilt on each miss.
_ON_BRAND_RATIONALE = "Asset aligns with declared audience and brand language."
_OFF_BRAND_RATIONALE = "Asset is missing core brand signals."
_REVISION_SUGGESTIONS = (
    "Add clearer reference to target audience and expected outcome.",
    "Use approved voice-and-tone language from the writing playbook.",
    "Map copy to one narrative pillar and include proof.",
)


@dataclass
class BrandComplianceAgent:
    """Evaluates whether assets are on-brand via keyword matching against mission-derived
    brand signals (values, differentiators, company name, and target audience)."""

    role: str = "Brand Compliance Reviewer"

    def evaluate(
        self, checks: List[BrandCheckRequest], mission: BrandingMission
    ) -> List[BrandCheckResult]:
        """Evaluate each asset check against brand signals derived from the mission.

        Preconditions:
            - ``checks`` is an iterable of ``BrandCheckRequest`` instances, each
              providing ``asset_name`` and ``asset_description``.
            - ``mission`` provides ``values``, ``differentiators``, ``company_name``,
              and ``target_audience``.

        Postconditions:
            - Returns one ``BrandCheckResult`` per input check, in the same order
              as ``checks``.
            - ``is_on_brand`` is True iff at least two distinct brand keywords are
              matched (case-insensitive, word-boundary) in the asset's name and
              description.
            - ``confidence`` is ``min(0.95, 0.45 + 0.1 * len(matched))``, rounded
              to 2 decimal places.
            - ``revision_suggestions`` is empty when ``is_on_brand`` is True,
              otherwise the fixed set of revision suggestions.
        """
        keywords = [
            *mission.values,
            *mission.differentiators,
            mission.company_name,
            mission.target_audience,
        ]
        # Deduplicate (preserving order) so a keyword listed under more than one
        # mission field — e.g. also present as a differentiator — contributes at
        # most one match; otherwise a single distinct keyword could satisfy the
        # "at least two distinct keywords" on-brand threshold below.
        unique_keywords = list(dict.fromkeys(k for k in keywords if k))
        # Word-boundary patterns, compiled once per call. Substring matching
        # ("k in text") falsely fires on incidental overlaps — e.g. the value
        # "tech" matching "fintech" or "logistics" — inflating the on-brand
        # score. ``\b`` anchors each keyword (and multi-word phrase) to whole
        # words.
        patterns = [(k, re.compile(rf"\b{re.escape(k.lower())}\b")) for k in unique_keywords]
        results: List[BrandCheckResult] = []

        for check in checks:
            text = f"{check.asset_name} {check.asset_description}".lower()
            matched = [k for k, pat in patterns if pat.search(text)]
            is_on_brand = len(matched) >= 2
            confidence = min(0.95, 0.45 + (0.1 * len(matched)))

            rationale = [
                _ON_BRAND_RATIONALE if is_on_brand else _OFF_BRAND_RATIONALE,
                f"Detected brand cues: {', '.join(matched[:4]) or 'none'}.",
            ]
            revision_suggestions = [] if is_on_brand else list(_REVISION_SUGGESTIONS)

            results.append(
                BrandCheckResult(
                    asset_name=check.asset_name,
                    is_on_brand=is_on_brand,
                    confidence=round(confidence, 2),
                    rationale=rationale,
                    revision_suggestions=revision_suggestions,
                )
            )

        return results
