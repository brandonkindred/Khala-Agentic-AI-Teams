"""Agent factory functions for the branding team Strands SDK pipeline.

Each function returns a configured ``strands.Agent`` instance for use as a
node in a ``GraphBuilder`` graph.  Agents are grouped by phase.

``BrandComplianceAgent`` is the only non-Strands class; it runs
outside the graph as a post-processing utility.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List

from pydantic import BaseModel
from strands import Agent

from .graphs.shared import build_agent
from .models import (
    ApprovalWorkflowsOutput,
    AssetWikiOutput,
    AudienceSegmentsOutput,
    BrandArchetypesOutput,
    BrandArchitectureOutput,
    BrandCheckRequest,
    BrandCheckResult,
    BrandDiscoveryAuditOutput,
    BrandExperiencePrinciplesOutput,
    BrandGuidelinesOutput,
    BrandHealthKPIsOutput,
    BrandInActionOutput,
    BrandingMission,
    BrandStoryOutput,
    ChannelGuidelineOutput,
    ColorPaletteSystemOutput,
    CoreValuesOutput,
    CreativeRefinementDecisionOutput,
    DesignSystemDefinitionOutput,
    DifferentiationPillarsOutput,
    EvolutionFrameworkOutput,
    IconographyOutput,
    LogoSuiteOutput,
    MessagingFrameworkOutput,
    MoodBoardCandidatesOutput,
    MoodBoardConceptOutput,
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


def make_discovery_auditor() -> Agent:
    """Build the Phase 1 Discovery Auditor agent.

    Postconditions:
        Returns an ``Agent`` named ``discovery_auditor`` whose structured
        output is a ``BrandDiscoveryAuditOutput`` covering current brand
        perception, market position, SWOT, and stakeholder insights.
    """
    return build_agent(
        name="discovery_auditor",
        description="Analyses current brand perception, SWOT, and stakeholder insights.",
        system_prompt=(
            "You are a Brand Discovery Analyst. Given a branding mission, produce a comprehensive "
            "brand discovery audit. Include: current_brand_perception, market_position, strengths, "
            "weaknesses, opportunities, threats, and stakeholder_insights. Be specific and grounded "
            "in the company description and target audience provided."
        ),
        structured_output=BrandDiscoveryAuditOutput,
    )


_PURPOSE_VISION_PROMPT = AgentPromptSpec(
    opening="You are a Purpose & Vision Writer. Given a branding mission, write three things:",
    fields=(
        PromptFieldSpec("brand_purpose", "why the company exists (one sentence)"),
        PromptFieldSpec(
            "mission_statement", "what the company does for its audience (one sentence)"
        ),
        PromptFieldSpec("vision_statement", "the aspirational future state (one sentence)"),
    ),
    closing="Be concise, inspiring, and specific to the company.",
)


def make_purpose_vision_writer() -> Agent:
    """Build the Phase 1 Purpose & Vision Writer agent.

    Postconditions:
        Returns an ``Agent`` named ``purpose_vision_writer`` whose
        structured output is a ``PurposeVisionOutput`` containing the
        brand purpose, mission statement, and vision statement.
    """
    return build_agent(
        name="purpose_vision_writer",
        description="Crafts brand purpose, mission statement, and vision statement.",
        system_prompt=render_agent_prompt(_PURPOSE_VISION_PROMPT),
        structured_output=PurposeVisionOutput,
    )


def make_values_articulator() -> Agent:
    """Build the Phase 1 Values Articulator agent.

    Postconditions:
        Returns an ``Agent`` named ``values_articulator`` whose structured
        output is a ``CoreValuesOutput`` listing 3-5 core values, each with
        a behavioral definition and observable behaviors.
    """
    return build_agent(
        name="values_articulator",
        description="Defines core values with behavioral definitions and observable behaviors.",
        system_prompt=(
            "You are a Values Articulator. Given a branding mission with optional seed values, "
            "produce a list of 3-5 core values. For each value provide:\n"
            "- value: the value name\n"
            "- behavioral_definition: what this value means in practice\n"
            "- observable_behaviors: 2-3 concrete behaviors that demonstrate this value"
        ),
        structured_output=CoreValuesOutput,
    )


def make_audience_segmenter() -> Agent:
    """Build the Phase 1 Audience Segmenter agent.

    Postconditions:
        Returns an ``Agent`` named ``audience_segmenter`` whose structured
        output is an ``AudienceSegmentsOutput`` describing 1-3 target
        audience segments with pain points, goals, and decision drivers.
    """
    return build_agent(
        name="audience_segmenter",
        description="Segments target audience with psychographic depth.",
        system_prompt=(
            "You are an Audience Segmenter. Given a branding mission, identify 1-3 target audience "
            "segments. For each segment provide: name, description, pain_points (2-3), goals (2-3), "
            "and decision_drivers (2-3). Ground your analysis in the company description and stated "
            "target audience."
        ),
        structured_output=AudienceSegmentsOutput,
    )


def make_differentiation_mapper() -> Agent:
    """Build the Phase 1 Differentiation Mapper agent.

    Postconditions:
        Returns an ``Agent`` named ``differentiation_mapper`` whose
        structured output is a ``DifferentiationPillarsOutput`` listing
        2-4 differentiation pillars with proof points and competitive
        context.
    """
    return build_agent(
        name="differentiation_mapper",
        description="Maps competitive differentiation pillars with proof points.",
        system_prompt=(
            "You are a Differentiation Mapper. Given a branding mission with optional differentiators, "
            "produce 2-4 differentiation pillars. For each pillar provide:\n"
            "- pillar: the differentiator name\n"
            "- proof_points: 2-3 evidence items\n"
            "- competitive_context: how competitors fall short here"
        ),
        structured_output=DifferentiationPillarsOutput,
    )


def make_positioning_synthesizer() -> Agent:
    """Build the Phase 1 Positioning Synthesizer agent.

    Postconditions:
        Returns an ``Agent`` named ``positioning_synthesizer`` whose
        structured output is a ``PositioningOutput`` synthesizing the
        other Phase 1 fragments into a positioning statement and brand
        promise.
    """
    return build_agent(
        name="positioning_synthesizer",
        description="Synthesises all Phase 1 fragments into positioning statement and brand promise.",
        system_prompt=(
            "You are a Positioning Synthesizer. You receive outputs from the discovery auditor, "
            "purpose/vision writer, values articulator, audience segmenter, and differentiation "
            "mapper. Synthesise them into:\n"
            "1. positioning_statement — a single sentence following the format: "
            "'For [audience] who need [need], [company] is the [differentiator] that delivers "
            "[value] because [proof].'\n"
            "2. brand_promise — a one-sentence commitment to the customer"
        ),
        structured_output=PositioningOutput,
    )


# ===================================================================
# Phase 2 — Narrative & Messaging  (Graph: sequential specialists)
# ===================================================================


def make_storyteller() -> Agent:
    """Build the Phase 2 Storyteller agent.

    Postconditions:
        Returns an ``Agent`` named ``Storyteller`` whose structured output
        is a ``BrandStoryOutput`` containing the brand story, hero
        narrative, and boilerplate variants.
    """
    return build_agent(
        name="Storyteller",
        description="Crafts the brand story, hero narrative, and boilerplate variants.",
        system_prompt=(
            "You are a Brand Storyteller. Using the strategic core output and branding mission, "
            "craft:\n"
            "1. brand_story — a compelling 2-3 paragraph origin/purpose story\n"
            "2. hero_narrative — a shorter, punchy version for hero sections\n"
            "3. boilerplate_variants — 3 versions (short/medium/long) for press and bios"
        ),
        structured_output=BrandStoryOutput,
    )


def make_archetype_analyst() -> Agent:
    """Build the Phase 2 Archetype Analyst agent.

    Postconditions:
        Returns an ``Agent`` named ``ArchetypeAnalyst`` whose structured
        output is a ``BrandArchetypesOutput`` selecting 1-2 brand
        archetypes with rationale and personality traits, carrying
        forward the prior narrative fields unchanged.
    """
    return build_agent(
        name="ArchetypeAnalyst",
        description="Selects brand archetypes with rationale and personality traits.",
        system_prompt=(
            "You are a Brand Archetype Analyst. Review the brand story from Inputs from previous "
            "nodes and the strategic core, then select 1-2 brand archetypes "
            "(e.g. The Sage, The Creator, The Explorer). Carry forward brand_story, hero_narrative, "
            "and boilerplate_variants unchanged, and add for each archetype:\n"
            "- archetype: name\n"
            "- rationale: why this fits\n"
            "- personality_traits: 3-5 traits"
        ),
        structured_output=BrandArchetypesOutput,
    )


def make_tagline_writer() -> Agent:
    """Build the Phase 2 Tagline Writer agent.

    Postconditions:
        Returns an ``Agent`` named ``TaglineWriter`` whose structured
        output is a ``TaglineOutput`` adding a tagline, tagline
        rationale, and elevator pitches to the prior narrative fields.
    """
    return build_agent(
        name="TaglineWriter",
        description="Creates tagline, tagline rationale, and elevator pitches.",
        system_prompt=(
            "You are a Tagline Writer. Using Inputs from previous nodes (brand story, archetypes) "
            "and the strategic core, carry forward every prior narrative field unchanged and add:\n"
            "1. tagline — a memorable brand tagline (max 8 words)\n"
            "2. tagline_rationale — why this tagline works\n"
            "3. elevator_pitches — three variants:\n"
            "   - tier: '5-second', pitch: ...\n"
            "   - tier: '30-second', pitch: ...\n"
            "   - tier: '2-minute', pitch: ..."
        ),
        structured_output=TaglineOutput,
    )


def make_message_mapper() -> Agent:
    """Build the Phase 2 Message Mapper agent.

    Postconditions:
        Returns an ``Agent`` named ``MessageMapper`` whose structured
        output is a ``MessagingFrameworkOutput`` adding a messaging
        framework and per-segment audience message maps to the prior
        narrative fields.
    """
    return build_agent(
        name="MessageMapper",
        description="Builds messaging framework pillars and audience message maps.",
        system_prompt=(
            "You are a Message Mapper. Using all prior narrative fields from Inputs from previous "
            "nodes, carry them forward unchanged and add:\n"
            "1. messaging_framework — 3-4 messaging pillars, each with:\n"
            "   - pillar, key_message, proof_points\n"
            "2. audience_message_maps — one per audience segment, each with:\n"
            "   - audience_segment, primary_message, supporting_messages, tone_adjustments"
        ),
        structured_output=MessagingFrameworkOutput,
    )


def make_persona_builder() -> Agent:
    """Build the Phase 2 Persona Builder agent.

    Postconditions:
        Returns an ``Agent`` named ``PersonaBuilder`` whose structured
        output is a ``PersonaProfilesOutput`` adding 2-3 persona profiles
        to the prior narrative fields.
    """
    return build_agent(
        name="PersonaBuilder",
        description="Creates rich persona profiles with psychographic depth.",
        system_prompt=(
            "You are a Persona Builder. Using audience segments and all prior narrative fields "
            "from Inputs from previous nodes, carry those fields forward unchanged and create "
            "2-3 persona profiles. Each persona has: name, role, demographics, psychographics, "
            "goals, frustrations, media_habits, jobs_to_be_done."
        ),
        structured_output=PersonaProfilesOutput,
    )


def make_voice_principles_drafter() -> Agent:
    """Build the Phase 2 Voice Principles Drafter agent.

    Postconditions:
        Returns an ``Agent`` named ``VoicePrinciplesDrafter`` whose
        structured output is a ``WritingGuidelinesOutput`` adding voice
        principles, style dos/don'ts, and an editorial quality bar to the
        prior narrative fields — the final step in narrative development.
    """
    return build_agent(
        name="VoicePrinciplesDrafter",
        description="Defines writing guidelines: voice principles, style dos/donts, editorial bar.",
        system_prompt=(
            "You are a Voice Principles Drafter. Using all prior narrative fields from Inputs from "
            "previous nodes and the mission's desired_voice, carry the prior fields forward "
            "unchanged and produce writing_guidelines:\n"
            "1. voice_principles — 3-4 principles (e.g. 'Use a confident, human voice')\n"
            "2. style_dos — 3-4 writing best practices\n"
            "3. style_donts — 3-4 things to avoid\n"
            "4. editorial_quality_bar — 3-4 quality standards every piece must meet\n\n"
            "This is the final step in narrative development."
        ),
        structured_output=WritingGuidelinesOutput,
    )


# ===================================================================
# Phase 3 — Visual & Expressive Identity  (Graph: diverge fan-out + converge fan-out)
# ===================================================================

# --- Diverge fan-out agents ---


def make_creative_director() -> Agent:
    """Build the Phase 3 Creative Director agent.

    Postconditions:
        Returns an ``Agent`` named ``CreativeDirector`` that collects the
        moodboard concepts produced by the ``MoodBoardConceptualist_*``
        Graph fan-out nodes into a unified ``MoodBoardCandidatesOutput``
        list, preserving each concept's title, visual_direction,
        color_story, typography_direction, and image_style. The agent does
        not select a winner; ``converge_decider`` scores the candidates
        and selects the winning direction.
    """
    return build_agent(
        name="CreativeDirector",
        description="Collects moodboard candidates from conceptualists into a unified list.",
        system_prompt=(
            "You are a Creative Director reviewing visual identity exploration. Using the three "
            "moodboard concepts from Inputs from previous nodes, collect them into "
            "mood_board_candidates. Preserve each concept (title, visual_direction, color_story, "
            "typography_direction, image_style). Do not pick a winner — converge_decider selects "
            "the winning direction."
        ),
        structured_output=MoodBoardCandidatesOutput,
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
        typography direction, image style) for the ``CreativeDirector``
        node in the Phase 3 Graph.
    """
    assert isinstance(variant, str) and variant.strip(), "variant must be a non-empty string"
    return build_agent(
        name=f"MoodBoardConceptualist_{variant}",
        description=f"Generates a {variant.lower()} visual direction moodboard concept.",
        system_prompt=(
            f"You are a MoodBoard Conceptualist specialising in {variant.lower()} visual "
            f"directions. Given a brand's strategic core and narrative, create a moodboard concept "
            f"with:\n"
            f"- title: a name for this direction\n"
            f"- visual_direction: overall aesthetic description\n"
            f"- color_story: 3-4 color names/descriptions\n"
            f"- typography_direction: font style recommendations\n"
            f"- image_style: 3-4 image style descriptions"
        ),
        structured_output=MoodBoardConceptOutput,
    )


# --- Post-diverge Graph nodes ---


def make_converge_decider() -> Agent:
    """Build the Phase 3 Converge Decider agent.

    Postconditions:
        Returns an ``Agent`` named ``converge_decider`` that scores the
        diverge-phase moodboard candidates against audience resonance,
        distinctiveness, cross-channel consistency, and feasibility, and
        selects a winning candidate.
    """
    return build_agent(
        name="converge_decider",
        description="Scores moodboard candidates and selects a winner.",
        system_prompt=(
            "You are a Creative Convergence Decider. You receive moodboard candidates from the "
            "diverge phase plus the brand's strategic core and values. Score each candidate on:\n"
            "- Audience resonance\n"
            "- Distinctiveness vs competitors\n"
            "- Cross-channel consistency\n"
            "- Execution feasibility\n\n"
            "Produce: winning_candidate_title, scoring_criteria, scores_by_candidate (dict of "
            "title→score), rationale, workshop_prompts (3 questions for stakeholders), and "
            "decision_criteria used."
        ),
        structured_output=CreativeRefinementDecisionOutput,
    )


def make_logo_specifier() -> Agent:
    """Build the Phase 3 Logo Specifier agent.

    Postconditions:
        Returns an ``Agent`` named ``logo_specifier`` whose output
        defines the logo suite (primary, monochrome, icon-only,
        reversed variants) with usage rules for the winning moodboard
        direction.
    """
    return build_agent(
        name="logo_specifier",
        description="Defines logo suite with usage rules.",
        system_prompt=(
            "You are a Logo Specifier. Based on the winning moodboard direction, define a logo "
            "suite. For each variant (primary, monochrome, icon-only, reversed), specify:\n"
            "- variant, usage_context, minimum_size, clear_space"
        ),
        structured_output=LogoSuiteOutput,
    )


def make_color_system_builder() -> Agent:
    """Build the Phase 3 Color System Builder agent.

    Postconditions:
        Returns an ``Agent`` named ``color_system_builder`` whose output
        defines 5-7 brand colors with hex values, usage, and
        psychological rationale for the winning moodboard direction.
    """
    return build_agent(
        name="color_system_builder",
        description="Builds the brand color palette with psychological rationale.",
        system_prompt=(
            "You are a Color System Builder. Based on the winning moodboard direction, define "
            "5-7 colors. For each: name, hex_value, usage (where to use it), and "
            "psychological_rationale (why this color works for the brand). Include primary, "
            "secondary, accent, surface, and critical colors."
        ),
        structured_output=ColorPaletteSystemOutput,
    )


def make_typography_builder() -> Agent:
    """Build the Phase 3 Typography Builder agent.

    Postconditions:
        Returns an ``Agent`` named ``typography_builder`` whose output
        defines a typography system of 3-4 type roles for the winning
        moodboard direction.
    """
    return build_agent(
        name="typography_builder",
        description="Defines the typography system.",
        system_prompt=(
            "You are a Typography Builder. Based on the winning moodboard direction, define a "
            "typography system with 3-4 type roles (display, body, caption, code). For each:\n"
            "- role, font_family, weight_range, usage_notes"
        ),
        structured_output=TypographySystemOutput,
    )


_ICONOGRAPHY_PROMPT = AgentPromptSpec(
    opening="You are an Iconography Director. Based on the winning moodboard, define:",
    fields=(
        PromptFieldSpec(
            "iconography_style", "describe the icon aesthetic (line weight, corner radius, fill)"
        ),
        PromptFieldSpec(
            "illustration_style", "describe the illustration approach (flat, isometric, etc.)"
        ),
    ),
)


def make_iconography_director() -> Agent:
    """Build the Phase 3 Iconography Director agent.

    Postconditions:
        Returns an ``Agent`` named ``iconography_director`` whose output
        defines the iconography and illustration style for the winning
        moodboard direction.
    """
    return build_agent(
        name="iconography_director",
        description="Defines iconography and illustration style.",
        system_prompt=render_agent_prompt(_ICONOGRAPHY_PROMPT),
        structured_output=IconographyOutput,
    )


def make_photography_video_director() -> Agent:
    """Build the Phase 3 Photography & Video Director agent.

    Postconditions:
        Returns an ``Agent`` named ``photography_video_director`` whose
        output defines photography direction, video direction, and
        motion principles for the winning moodboard direction.
    """
    return build_agent(
        name="photography_video_director",
        description="Defines photography direction, video direction, and motion principles.",
        system_prompt=(
            "You are a Photography & Video Director. Based on the winning moodboard, define:\n"
            "1. photography_direction — shooting style, lighting, composition, subjects\n"
            "2. video_direction — pacing, tone, visual style for video content\n"
            "3. motion_principles — 3-4 principles for animation/motion design"
        ),
        structured_output=PhotographyVideoOutput,
    )


def make_voice_tone_builder() -> Agent:
    """Build the Phase 3 Voice & Tone Builder agent.

    Postconditions:
        Returns an ``Agent`` named ``voice_tone_builder`` whose output
        defines the voice/tone spectrum and language dos/don'ts, drawing
        on the brand narrative's writing guidelines and the moodboard
        direction.
    """
    return build_agent(
        name="voice_tone_builder",
        description="Defines voice/tone spectrum and language dos/donts.",
        system_prompt=(
            "You are a Voice & Tone Builder. Using the brand narrative's writing guidelines and "
            "the moodboard direction, define:\n"
            "1. voice_tone_spectrum — for each context (marketing, support, legal, social, "
            "internal), specify the tone and 2-3 examples\n"
            "2. language_dos — 4-5 approved language patterns\n"
            "3. language_donts — 4-5 language anti-patterns"
        ),
        structured_output=VoiceToneOutput,
    )


def make_design_system_codifier() -> Agent:
    """Build the Phase 3 Design System Codifier agent.

    Postconditions:
        Returns an ``Agent`` named ``design_system_codifier`` whose
        output codifies design principles, foundation tokens, and
        component standards from the full visual identity work.
    """
    return build_agent(
        name="design_system_codifier",
        description="Codifies the design system: principles, tokens, component standards.",
        system_prompt=(
            "You are a Design System Codifier. Based on the full visual identity work, produce:\n"
            "1. design_principles — 3-4 guiding principles (e.g. 'Clarity over decoration')\n"
            "2. foundation_tokens — 4-6 token categories (color, type, spacing, motion, etc.)\n"
            "3. component_standards — 3-5 component rules (buttons, cards, navigation, etc.)"
        ),
        structured_output=DesignSystemDefinitionOutput,
    )


# ===================================================================
# Phase 4 — Experience & Channel Activation  (Graph: fan-out / fan-in)
# ===================================================================


def make_brand_experience_principler() -> Agent:
    """Build the Phase 4 Brand Experience Principler agent.

    Postconditions:
        Returns an ``Agent`` named ``brand_experience_principler`` whose
        output defines brand experience principles, signature customer
        journey moments, and sensory elements.
    """
    return build_agent(
        name="brand_experience_principler",
        description="Defines brand experience principles, signature moments, and sensory elements.",
        system_prompt=(
            "You are a Brand Experience Architect. Define:\n"
            "1. brand_experience_principles — 3-5 principles that govern every brand touchpoint\n"
            "2. signature_moments — 3-5 key moments in the customer journey that should feel "
            "distinctly on-brand\n"
            "3. sensory_elements — 2-4 sensory cues (sound, texture, scent, etc.) if applicable"
        ),
        structured_output=BrandExperiencePrinciplesOutput,
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
        types, and cadence per ``structured_output``.
    """
    assert isinstance(channel, str) and channel.strip(), "channel must be a non-empty string"
    assert isinstance(description, str) and description.strip(), (
        "description must be a non-empty string"
    )
    assert isinstance(structured_output, type) and issubclass(structured_output, BaseModel), (
        "structured_output must be a Pydantic BaseModel subclass"
    )
    return build_agent(
        name=f"{channel}_guide",
        description=f"Defines brand guidelines for the {channel} channel.",
        system_prompt=(
            f"You are a {channel.title()} Channel Specialist. Define guidelines for the "
            f"{channel} channel:\n"
            f"- channel: '{channel}'\n"
            f"- strategy: overall approach for this channel\n"
            f"- dos: 3-4 best practices\n"
            f"- donts: 3-4 things to avoid\n"
            f"- content_types: 3-5 recommended content formats\n"
            f"- frequency_guidance: recommended cadence\n"
            f"Context: {description}"
        ),
        structured_output=structured_output,
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


def make_brand_architecture_builder() -> Agent:
    """Build the Phase 4 Brand Architecture Specialist agent.

    Postconditions:
        Returns an ``Agent`` named ``brand_architecture_builder`` whose
        output defines brand architecture rules, naming conventions,
        and a terminology glossary.
    """
    return build_agent(
        name="brand_architecture_builder",
        description="Defines brand architecture rules, naming conventions, and terminology.",
        system_prompt=(
            "You are a Brand Architecture Specialist. Define:\n"
            "1. brand_architecture — rules for parent brand, sub-brands, product lines. Each "
            "with: entity, relationship, naming_convention, visual_treatment\n"
            "2. naming_conventions — 3-5 naming rules\n"
            "3. terminology_glossary — 5-10 key terms with definitions (dict)\n"
        ),
        structured_output=BrandArchitectureOutput,
    )


def make_brand_in_action_illustrator() -> Agent:
    """Build the Phase 4 Brand-in-Action Illustrator agent.

    Postconditions:
        Returns an ``Agent`` named ``brand_in_action_illustrator`` whose
        output produces 3-5 correct-vs-incorrect brand usage examples.
    """
    return build_agent(
        name="brand_in_action_illustrator",
        description="Creates brand-in-action do/don't examples.",
        system_prompt=(
            "You are a Brand-in-Action Illustrator. Create 3-5 applied examples showing correct "
            "vs incorrect brand usage. Each example has:\n"
            "- context: where this applies (e.g. 'sales deck header')\n"
            "- correct_example: the on-brand version\n"
            "- incorrect_example: the off-brand version\n"
            "- rationale: why the correct version is better\n"
        ),
        structured_output=BrandInActionOutput,
    )


# ===================================================================
# Phase 5 — Governance & Evolution  (Graph: fan-out / fan-in)
# ===================================================================


def make_ownership_definer() -> Agent:
    """Build the Phase 5 Brand Ownership Definer agent.

    Postconditions:
        Returns an ``Agent`` named ``ownership_definer`` whose output
        defines the brand ownership model and a decision authority
        matrix.
    """
    return build_agent(
        name="ownership_definer",
        description="Defines brand ownership model and decision authority matrix.",
        system_prompt=(
            "You are a Brand Ownership Definer. Define:\n"
            "1. ownership_model — who owns the brand (paragraph)\n"
            "2. decision_authority — a dict mapping decision types to responsible roles "
            "(e.g. 'logo_changes': 'Brand Director', 'campaign_messaging': 'Marketing Lead')"
        ),
        structured_output=OwnershipOutput,
    )


def make_approval_workflow_designer() -> Agent:
    """Build the Phase 5 Approval Workflow Designer agent.

    Postconditions:
        Returns an ``Agent`` named ``approval_workflow_designer`` whose
        output defines approval workflows and agency briefing
        protocols.
    """
    return build_agent(
        name="approval_workflow_designer",
        description="Designs approval workflows and agency briefing protocols.",
        system_prompt=(
            "You are an Approval Workflow Designer. Define:\n"
            "1. approval_workflows — 3-5 workflows, each with: asset_type, approvers (list), "
            "sla, escalation_path\n"
            "2. agency_briefing_protocols — 3-5 protocols for briefing external agencies"
        ),
        structured_output=ApprovalWorkflowsOutput,
    )


def make_asset_wiki_planner() -> Agent:
    """Build the Phase 5 Asset & Wiki Planner agent.

    Postconditions:
        Returns an ``Agent`` named ``asset_wiki_planner`` whose output
        defines asset management guidance and the brand wiki backlog.
    """
    return build_agent(
        name="asset_wiki_planner",
        description="Plans asset management and brand wiki backlog.",
        system_prompt=(
            "You are an Asset & Wiki Planner. Define:\n"
            "1. asset_management_guidance — 3-5 guidelines for managing brand assets\n"
            "2. wiki_backlog — 4-6 wiki entries, each with: title, summary, owners (list), "
            "update_cadence. Cover: Brand North Star, Voice Playbook, Design System, Brand "
            "Review Intake, Channel Playbook, Governance Charter."
        ),
        structured_output=AssetWikiOutput,
    )


def make_training_planner() -> Agent:
    """Build the Phase 5 Training Planner agent.

    Postconditions:
        Returns an ``Agent`` named ``training_planner`` whose output
        defines the brand training and onboarding plan.
    """
    return build_agent(
        name="training_planner",
        description="Plans brand training and onboarding programmes.",
        system_prompt=(
            "You are a Training Planner. Define training_onboarding_plan — 4-6 training "
            "initiatives for onboarding new team members and maintaining brand literacy."
        ),
        structured_output=TrainingOnboardingOutput,
    )


def make_kpi_designer() -> Agent:
    """Build the Phase 5 Brand KPI Designer agent.

    Postconditions:
        Returns an ``Agent`` named ``kpi_designer`` whose output defines
        brand health KPIs, tracking methodology, and review trigger
        points.
    """
    return build_agent(
        name="kpi_designer",
        description="Designs brand health KPIs with tracking methodology.",
        system_prompt=(
            "You are a Brand KPI Designer. Define:\n"
            "1. brand_health_kpis — 4-6 KPIs, each with: metric, measurement_method, target, "
            "review_frequency\n"
            "2. tracking_methodology — paragraph describing the measurement approach\n"
            "3. review_trigger_points — 3-5 events that should trigger a brand health review"
        ),
        structured_output=BrandHealthKPIsOutput,
    )


def make_evolution_framer() -> Agent:
    """Build the Phase 5 Brand Evolution Framer agent.

    Postconditions:
        Returns an ``Agent`` named ``evolution_framer`` whose output
        defines the brand evolution framework and version control
        cadence.
    """
    return build_agent(
        name="evolution_framer",
        description="Defines the brand evolution framework and version control cadence.",
        system_prompt=(
            "You are a Brand Evolution Framer. Define:\n"
            "1. evolution_framework — paragraph describing how the brand evolves over time\n"
            "2. version_control_cadence — how often the brand system is formally reviewed "
            "and versioned"
        ),
        structured_output=EvolutionFrameworkOutput,
    )


def make_brand_rules_codifier() -> Agent:
    """Build the Phase 5 Brand Rules Codifier agent.

    Postconditions:
        Returns an ``Agent`` named ``brand_rules_codifier`` whose output
        codifies 5-8 top-level brand governance rules.
    """
    return build_agent(
        name="brand_rules_codifier",
        description="Codifies top-level brand governance rules.",
        system_prompt=(
            "You are a Brand Rules Codifier. Using the full brand context (positioning, promise, "
            "values, narrative, visual identity), produce brand_guidelines — a list of 5-8 "
            "governance rules that everyone in the organisation must follow. Each rule is a "
            "single clear sentence. Cover: identity usage, messaging hierarchy, approval gates, "
            "asset management, and evolution."
        ),
        structured_output=BrandGuidelinesOutput,
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
