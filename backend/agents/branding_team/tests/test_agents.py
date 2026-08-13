"""Snapshot-fidelity tests for migrated branding agent system prompts.

Locks the exact rendered ``system_prompt`` of every factory migrated to the
data-driven ``AgentPromptSpec``/``render_agent_prompt`` pattern
(``branding_team.prompt_spec``) against the original hand-written
prose, so an accidental wording change in a spec constant is caught here
rather than silently drifting.
"""

from __future__ import annotations

from branding_team.agents import (
    make_approval_workflow_designer,
    make_archetype_analyst,
    make_asset_wiki_planner,
    make_audience_segmenter,
    make_brand_architecture_builder,
    make_brand_experience_principler,
    make_brand_in_action_illustrator,
    make_brand_rules_codifier,
    make_color_system_builder,
    make_converge_decider,
    make_creative_director,
    make_design_system_codifier,
    make_differentiation_mapper,
    make_discovery_auditor,
    make_email_guide,
    make_events_guide,
    make_evolution_framer,
    make_iconography_director,
    make_internal_guide,
    make_kpi_designer,
    make_logo_specifier,
    make_message_mapper,
    make_moodboard_conceptualist,
    make_ownership_definer,
    make_partnerships_guide,
    make_persona_builder,
    make_photography_video_director,
    make_positioning_synthesizer,
    make_purpose_vision_writer,
    make_social_guide,
    make_storyteller,
    make_tagline_writer,
    make_training_planner,
    make_typography_builder,
    make_values_articulator,
    make_voice_principles_drafter,
    make_voice_tone_builder,
    make_website_guide,
)

_EXPECTED_PURPOSE_VISION_PROMPT = (
    "You are a Purpose & Vision Writer. Given a branding mission, write three things:\n"
    "1. brand_purpose — why the company exists (one sentence)\n"
    "2. mission_statement — what the company does for its audience (one sentence)\n"
    "3. vision_statement — the aspirational future state (one sentence)\n"
    "Be concise, inspiring, and specific to the company."
)

_EXPECTED_ICONOGRAPHY_PROMPT = (
    "You are an Iconography Director. Based on the winning moodboard, define:\n"
    "1. iconography_style — describe the icon aesthetic (line weight, corner radius, fill)\n"
    "2. illustration_style — describe the illustration approach (flat, isometric, etc.)"
)

_EXPECTED_DISCOVERY_AUDITOR_PROMPT = (
    "You are a Brand Discovery Analyst. Given a branding mission, produce a comprehensive "
    "brand discovery audit.\n"
    "1. current_brand_perception — how the brand is currently perceived by its audience and market\n"
    "2. market_position — where the brand sits relative to competitors today\n"
    "3. strengths — the brand's key strengths\n"
    "4. weaknesses — the brand's key weaknesses\n"
    "5. opportunities — opportunities the brand can pursue\n"
    "6. threats — threats the brand faces\n"
    "7. stakeholder_insights — insights gathered from stakeholders\n"
    "Be specific and grounded in the company description and target audience provided."
)

_EXPECTED_VALUES_ARTICULATOR_PROMPT = (
    "You are a Values Articulator. Given a branding mission with optional seed values, "
    "produce a list of 3-5 core values.\n"
    "1. core_values — for each value provide: value (the value name), behavioral_definition "
    "(what this value means in practice), and observable_behaviors (2-3 concrete behaviors "
    "that demonstrate this value)"
)

_EXPECTED_AUDIENCE_SEGMENTER_PROMPT = (
    "You are an Audience Segmenter. Given a branding mission, identify 1-3 target audience segments.\n"
    "1. target_audience_segments — for each segment provide: name, description, pain_points "
    "(2-3), goals (2-3), and decision_drivers (2-3)\n"
    "Ground your analysis in the company description and stated target audience."
)

_EXPECTED_DIFFERENTIATION_MAPPER_PROMPT = (
    "You are a Differentiation Mapper. Given a branding mission with optional differentiators, "
    "produce 2-4 differentiation pillars.\n"
    "1. differentiation_pillars — for each pillar provide: pillar (the differentiator name), "
    "proof_points (2-3 evidence items), and competitive_context (how competitors fall short here)"
)

_EXPECTED_POSITIONING_SYNTHESIZER_PROMPT = (
    "You are a Positioning Synthesizer. You receive outputs from the discovery auditor, "
    "purpose/vision writer, values articulator, audience segmenter, and differentiation "
    "mapper. Synthesise them into:\n"
    "1. positioning_statement — a single sentence following the format: "
    "'For [audience] who need [need], [company] is the [differentiator] that delivers "
    "[value] because [proof].'\n"
    "2. brand_promise — a one-sentence commitment to the customer"
)

_EXPECTED_STORYTELLER_PROMPT = (
    "You are a Brand Storyteller. Using the strategic core output and branding mission, "
    "craft:\n"
    "1. brand_story — a compelling 2-3 paragraph origin/purpose story\n"
    "2. hero_narrative — a shorter, punchy version for hero sections\n"
    "3. boilerplate_variants — 3 versions (short/medium/long) for press and bios"
)

_EXPECTED_ARCHETYPE_ANALYST_PROMPT = (
    "You are a Brand Archetype Analyst. Review the brand story from Inputs from previous "
    "nodes and the strategic core, then select 1-2 brand archetypes (e.g. The Sage, The "
    "Creator, The Explorer). Carry forward brand_story, hero_narrative, and "
    "boilerplate_variants unchanged, and add:\n"
    "1. brand_archetypes — for each archetype: archetype (name), rationale (why this fits), "
    "and personality_traits (3-5 traits)"
)

_EXPECTED_TAGLINE_WRITER_PROMPT = (
    "You are a Tagline Writer. Using Inputs from previous nodes (brand story, archetypes) "
    "and the strategic core, carry forward every prior narrative field unchanged and add:\n"
    "1. tagline — a memorable brand tagline (max 8 words)\n"
    "2. tagline_rationale — why this tagline works\n"
    "3. elevator_pitches — three variants: tier '5-second' pitch, tier '30-second' pitch, "
    "and tier '2-minute' pitch"
)

_EXPECTED_MESSAGE_MAPPER_PROMPT = (
    "You are a Message Mapper. Using all prior narrative fields from Inputs from previous "
    "nodes, carry them forward unchanged and add:\n"
    "1. messaging_framework — 3-4 messaging pillars, each with: pillar, key_message, and proof_points\n"
    "2. audience_message_maps — one per audience segment, each with: audience_segment, "
    "primary_message, supporting_messages, and tone_adjustments"
)

_EXPECTED_PERSONA_BUILDER_PROMPT = (
    "You are a Persona Builder. Using audience segments and all prior narrative fields "
    "from Inputs from previous nodes, carry those fields forward unchanged and create:\n"
    "1. persona_profiles — 2-3 persona profiles, each with: name, role, demographics, "
    "psychographics, goals, frustrations, media_habits, jobs_to_be_done"
)

_EXPECTED_VOICE_PRINCIPLES_DRAFTER_PROMPT = (
    "You are a Voice Principles Drafter. Using all prior narrative fields from Inputs from "
    "previous nodes and the mission's desired_voice, carry the prior fields forward "
    "unchanged and produce writing_guidelines:\n"
    "1. voice_principles — 3-4 principles (e.g. 'Use a confident, human voice')\n"
    "2. style_dos — 3-4 writing best practices\n"
    "3. style_donts — 3-4 things to avoid\n"
    "4. editorial_quality_bar — 3-4 quality standards every piece must meet\n\n"
    "This is the final step in narrative development."
)

_EXPECTED_PHOTOGRAPHY_VIDEO_DIRECTOR_PROMPT = (
    "You are a Photography & Video Director. Based on the winning moodboard, define:\n"
    "1. photography_direction — shooting style, lighting, composition, subjects\n"
    "2. video_direction — pacing, tone, visual style for video content\n"
    "3. motion_principles — 3-4 principles for animation/motion design"
)

_EXPECTED_VOICE_TONE_BUILDER_PROMPT = (
    "You are a Voice & Tone Builder. Using the brand narrative's writing guidelines and "
    "the moodboard direction, define:\n"
    "1. voice_tone_spectrum — for each context (marketing, support, legal, social, "
    "internal), specify the tone and 2-3 examples\n"
    "2. language_dos — 4-5 approved language patterns\n"
    "3. language_donts — 4-5 language anti-patterns"
)

_EXPECTED_DESIGN_SYSTEM_CODIFIER_PROMPT = (
    "You are a Design System Codifier. Based on the full visual identity work, produce:\n"
    "1. design_principles — 3-4 guiding principles (e.g. 'Clarity over decoration')\n"
    "2. foundation_tokens — 4-6 token categories (color, type, spacing, motion, etc.)\n"
    "3. component_standards — 3-5 component rules (buttons, cards, navigation, etc.)"
)


def _expected_moodboard_conceptualist_prompt(variant: str) -> str:
    return (
        f"You are a MoodBoard Conceptualist specialising in {variant.lower()} visual "
        f"directions. Given a brand's strategic core and narrative, create a moodboard "
        f"concept with:\n"
        f"1. title — a name for this direction\n"
        f"2. visual_direction — overall aesthetic description\n"
        f"3. color_story — 3-4 color names/descriptions\n"
        f"4. typography_direction — font style recommendations\n"
        f"5. image_style — 3-4 image style descriptions"
    )


_EXPECTED_MOODBOARD_EDITORIAL_PROMPT = _expected_moodboard_conceptualist_prompt("Editorial")
_EXPECTED_MOODBOARD_MINIMALIST_PROMPT = _expected_moodboard_conceptualist_prompt("Minimalist")
_EXPECTED_MOODBOARD_BOLD_PROMPT = _expected_moodboard_conceptualist_prompt("Bold")

_EXPECTED_CREATIVE_DIRECTOR_PROMPT = (
    "You are a Creative Director reviewing visual identity exploration. Using the three "
    "moodboard concepts from Inputs from previous nodes, collect them into:\n"
    "1. mood_board_candidates — preserve each concept (title, visual_direction, color_story, "
    "typography_direction, image_style)\n"
    "Do not pick a winner — converge_decider selects the winning direction."
)

_EXPECTED_CONVERGE_DECIDER_PROMPT = (
    "You are a Creative Convergence Decider. You receive moodboard candidates from the "
    "diverge phase plus the brand's strategic core and values. Score each candidate on:\n"
    "- Audience resonance\n"
    "- Distinctiveness vs competitors\n"
    "- Cross-channel consistency\n"
    "- Execution feasibility\n\n"
    "Produce:\n"
    "1. winning_candidate_title — the selected candidate title\n"
    "2. scoring_criteria — the criteria used to score candidates\n"
    "3. scores_by_candidate — dict of title→score\n"
    "4. rationale — why this candidate won\n"
    "5. workshop_prompts — 3 questions for stakeholders\n"
    "6. decision_criteria — decision criteria used"
)

_EXPECTED_LOGO_SPECIFIER_PROMPT = (
    "You are a Logo Specifier. Based on the winning moodboard direction, define a logo "
    "suite. For each variant (primary, monochrome, icon-only, reversed), specify:\n"
    "1. logo_suite — variant, usage_context, minimum_size, clear_space"
)

_EXPECTED_COLOR_SYSTEM_BUILDER_PROMPT = (
    "You are a Color System Builder. Based on the winning moodboard direction, define "
    "5-7 colors. Include primary, secondary, accent, surface, and critical colors.\n"
    "1. color_palette — for each: name, hex_value, usage (where to use it), and "
    "psychological_rationale (why this color works for the brand)"
)

_EXPECTED_TYPOGRAPHY_BUILDER_PROMPT = (
    "You are a Typography Builder. Based on the winning moodboard direction, define a "
    "typography system with 3-4 type roles (display, body, caption, code). For each:\n"
    "1. typography_system — role, font_family, weight_range, usage_notes"
)

_EXPECTED_BRAND_EXPERIENCE_PRINCIPLER_PROMPT = (
    "You are a Brand Experience Architect. Define:\n"
    "1. brand_experience_principles — 3-5 principles that govern every brand touchpoint\n"
    "2. signature_moments — 3-5 key moments in the customer journey that should feel "
    "distinctly on-brand\n"
    "3. sensory_elements — 2-4 sensory cues (sound, texture, scent, etc.) if applicable"
)

_EXPECTED_BRAND_ARCHITECTURE_BUILDER_PROMPT = (
    "You are a Brand Architecture Specialist. Define:\n"
    "1. brand_architecture — rules for parent brand, sub-brands, product lines. Each "
    "with: entity, relationship, naming_convention, visual_treatment\n"
    "2. naming_conventions — 3-5 naming rules\n"
    "3. terminology_glossary — 5-10 key terms with definitions (dict)"
)

_EXPECTED_OWNERSHIP_DEFINER_PROMPT = (
    "You are a Brand Ownership Definer. Define:\n"
    "1. ownership_model — who owns the brand (paragraph)\n"
    "2. decision_authority — a dict mapping decision types to responsible roles "
    "(e.g. 'logo_changes': 'Brand Director', 'campaign_messaging': 'Marketing Lead')"
)

_EXPECTED_APPROVAL_WORKFLOW_DESIGNER_PROMPT = (
    "You are an Approval Workflow Designer. Define:\n"
    "1. approval_workflows — 3-5 workflows, each with: asset_type, approvers (list), "
    "sla, escalation_path\n"
    "2. agency_briefing_protocols — 3-5 protocols for briefing external agencies"
)

_EXPECTED_ASSET_WIKI_PLANNER_PROMPT = (
    "You are an Asset & Wiki Planner. Define:\n"
    "1. asset_management_guidance — 3-5 guidelines for managing brand assets\n"
    "2. wiki_backlog — 4-6 wiki entries, each with: title, summary, owners (list), "
    "update_cadence. Cover: Brand North Star, Voice Playbook, Design System, Brand "
    "Review Intake, Channel Playbook, Governance Charter."
)

_EXPECTED_KPI_DESIGNER_PROMPT = (
    "You are a Brand KPI Designer. Define:\n"
    "1. brand_health_kpis — 4-6 KPIs, each with: metric, measurement_method, target, "
    "review_frequency\n"
    "2. tracking_methodology — paragraph describing the measurement approach\n"
    "3. review_trigger_points — 3-5 events that should trigger a brand health review"
)

_EXPECTED_EVOLUTION_FRAMER_PROMPT = (
    "You are a Brand Evolution Framer. Define:\n"
    "1. evolution_framework — paragraph describing how the brand evolves over time\n"
    "2. version_control_cadence — how often the brand system is formally reviewed "
    "and versioned"
)


# Dash-colon field bullets (`- channel: 'website'`) become numbered em-dash
# lines; ``Context: {description}`` is the spec closing sentence. Same
# conversion MoodBoardConceptualist used for its parameterized variants.
def _expected_channel_guide_prompt(channel: str, description: str) -> str:
    return (
        f"You are a {channel.title()} Channel Specialist. Define guidelines for the "
        f"{channel} channel:\n"
        f"1. channel — '{channel}'\n"
        f"2. strategy — overall approach for this channel\n"
        f"3. dos — 3-4 best practices\n"
        f"4. donts — 3-4 things to avoid\n"
        f"5. content_types — 3-5 recommended content formats\n"
        f"6. frequency_guidance — recommended cadence\n"
        f"Context: {description}"
    )


_EXPECTED_WEBSITE_GUIDE_PROMPT = _expected_channel_guide_prompt(
    "website", "Company website, landing pages, product pages."
)
_EXPECTED_SOCIAL_GUIDE_PROMPT = _expected_channel_guide_prompt(
    "social", "Social media platforms (LinkedIn, Twitter, Instagram)."
)
_EXPECTED_EMAIL_GUIDE_PROMPT = _expected_channel_guide_prompt(
    "email", "Email marketing, newsletters, transactional emails."
)
_EXPECTED_EVENTS_GUIDE_PROMPT = _expected_channel_guide_prompt(
    "events", "Conferences, webinars, meetups, trade shows."
)
_EXPECTED_PARTNERSHIPS_GUIDE_PROMPT = _expected_channel_guide_prompt(
    "partnerships", "Co-branding, sponsorships, partner marketing."
)
_EXPECTED_INTERNAL_GUIDE_PROMPT = _expected_channel_guide_prompt(
    "internal", "Internal comms, employee branding, onboarding."
)

# Nested dash-colon bullets (`- context: ...`) collapsed into the
# ``brand_in_action`` field description, matching Phase 3 logo/color nested
# member style. Trailing newline dropped (renderer has none).
_EXPECTED_BRAND_IN_ACTION_ILLUSTRATOR_PROMPT = (
    "You are a Brand-in-Action Illustrator. Create 3-5 applied examples showing correct "
    "vs incorrect brand usage:\n"
    "1. brand_in_action — each example has: context (where this applies, e.g. 'sales deck "
    "header'), correct_example (the on-brand version), incorrect_example (the off-brand "
    "version), rationale (why the correct version is better)"
)

# Original was a single unnumbered sentence; the renderer requires a numbered
# field line, so ``Define training_onboarding_plan —`` becomes ``Define:`` plus
# ``1. training_onboarding_plan —``.
_EXPECTED_TRAINING_PLANNER_PROMPT = (
    "You are a Training Planner. Define:\n"
    "1. training_onboarding_plan — 4-6 training initiatives for onboarding new team "
    "members and maintaining brand literacy."
)

# ``produce brand_guidelines — ...`` split into a numbered field; the ``Cover:``
# clause moves to the spec closing sentence.
_EXPECTED_BRAND_RULES_CODIFIER_PROMPT = (
    "You are a Brand Rules Codifier. Using the full brand context (positioning, promise, "
    "values, narrative, visual identity), produce:\n"
    "1. brand_guidelines — a list of 5-8 governance rules that everyone in the organisation "
    "must follow. Each rule is a single clear sentence.\n"
    "Cover: identity usage, messaging hierarchy, approval gates, asset management, and "
    "evolution."
)


def test_purpose_vision_writer_prompt_matches_original_wording() -> None:
    assert make_purpose_vision_writer().system_prompt == _EXPECTED_PURPOSE_VISION_PROMPT


def test_iconography_director_prompt_matches_original_wording() -> None:
    assert make_iconography_director().system_prompt == _EXPECTED_ICONOGRAPHY_PROMPT


def test_discovery_auditor_prompt_matches_original_wording() -> None:
    assert make_discovery_auditor().system_prompt == _EXPECTED_DISCOVERY_AUDITOR_PROMPT


def test_values_articulator_prompt_matches_original_wording() -> None:
    assert make_values_articulator().system_prompt == _EXPECTED_VALUES_ARTICULATOR_PROMPT


def test_audience_segmenter_prompt_matches_original_wording() -> None:
    assert make_audience_segmenter().system_prompt == _EXPECTED_AUDIENCE_SEGMENTER_PROMPT


def test_differentiation_mapper_prompt_matches_original_wording() -> None:
    assert make_differentiation_mapper().system_prompt == _EXPECTED_DIFFERENTIATION_MAPPER_PROMPT


def test_positioning_synthesizer_prompt_matches_original_wording() -> None:
    assert make_positioning_synthesizer().system_prompt == _EXPECTED_POSITIONING_SYNTHESIZER_PROMPT


def test_storyteller_prompt_matches_original_wording() -> None:
    assert make_storyteller().system_prompt == _EXPECTED_STORYTELLER_PROMPT


def test_archetype_analyst_prompt_matches_original_wording() -> None:
    assert make_archetype_analyst().system_prompt == _EXPECTED_ARCHETYPE_ANALYST_PROMPT


def test_tagline_writer_prompt_matches_original_wording() -> None:
    assert make_tagline_writer().system_prompt == _EXPECTED_TAGLINE_WRITER_PROMPT


def test_message_mapper_prompt_matches_original_wording() -> None:
    assert make_message_mapper().system_prompt == _EXPECTED_MESSAGE_MAPPER_PROMPT


def test_persona_builder_prompt_matches_original_wording() -> None:
    assert make_persona_builder().system_prompt == _EXPECTED_PERSONA_BUILDER_PROMPT


def test_voice_principles_drafter_prompt_matches_original_wording() -> None:
    assert (
        make_voice_principles_drafter().system_prompt == _EXPECTED_VOICE_PRINCIPLES_DRAFTER_PROMPT
    )


def test_photography_video_director_prompt_matches_original_wording() -> None:
    assert (
        make_photography_video_director().system_prompt
        == _EXPECTED_PHOTOGRAPHY_VIDEO_DIRECTOR_PROMPT
    )


def test_voice_tone_builder_prompt_matches_original_wording() -> None:
    assert make_voice_tone_builder().system_prompt == _EXPECTED_VOICE_TONE_BUILDER_PROMPT


def test_design_system_codifier_prompt_matches_original_wording() -> None:
    assert make_design_system_codifier().system_prompt == _EXPECTED_DESIGN_SYSTEM_CODIFIER_PROMPT


def test_moodboard_conceptualist_editorial_prompt_matches_spec() -> None:
    assert (
        make_moodboard_conceptualist("Editorial").system_prompt
        == _EXPECTED_MOODBOARD_EDITORIAL_PROMPT
    )


def test_moodboard_conceptualist_minimalist_prompt_matches_spec() -> None:
    assert (
        make_moodboard_conceptualist("Minimalist").system_prompt
        == _EXPECTED_MOODBOARD_MINIMALIST_PROMPT
    )


def test_moodboard_conceptualist_bold_prompt_matches_spec() -> None:
    assert make_moodboard_conceptualist("Bold").system_prompt == _EXPECTED_MOODBOARD_BOLD_PROMPT


def test_creative_director_prompt_matches_spec() -> None:
    assert make_creative_director().system_prompt == _EXPECTED_CREATIVE_DIRECTOR_PROMPT


def test_converge_decider_prompt_matches_spec() -> None:
    assert make_converge_decider().system_prompt == _EXPECTED_CONVERGE_DECIDER_PROMPT


def test_logo_specifier_prompt_matches_spec() -> None:
    assert make_logo_specifier().system_prompt == _EXPECTED_LOGO_SPECIFIER_PROMPT


def test_color_system_builder_prompt_matches_spec() -> None:
    assert make_color_system_builder().system_prompt == _EXPECTED_COLOR_SYSTEM_BUILDER_PROMPT


def test_typography_builder_prompt_matches_spec() -> None:
    assert make_typography_builder().system_prompt == _EXPECTED_TYPOGRAPHY_BUILDER_PROMPT


def test_brand_experience_principler_prompt_matches_original_wording() -> None:
    assert (
        make_brand_experience_principler().system_prompt
        == _EXPECTED_BRAND_EXPERIENCE_PRINCIPLER_PROMPT
    )


def test_brand_architecture_builder_prompt_matches_original_wording() -> None:
    assert (
        make_brand_architecture_builder().system_prompt
        == _EXPECTED_BRAND_ARCHITECTURE_BUILDER_PROMPT
    )


def test_ownership_definer_prompt_matches_original_wording() -> None:
    assert make_ownership_definer().system_prompt == _EXPECTED_OWNERSHIP_DEFINER_PROMPT


def test_approval_workflow_designer_prompt_matches_original_wording() -> None:
    assert (
        make_approval_workflow_designer().system_prompt
        == _EXPECTED_APPROVAL_WORKFLOW_DESIGNER_PROMPT
    )


def test_asset_wiki_planner_prompt_matches_original_wording() -> None:
    assert make_asset_wiki_planner().system_prompt == _EXPECTED_ASSET_WIKI_PLANNER_PROMPT


def test_kpi_designer_prompt_matches_original_wording() -> None:
    assert make_kpi_designer().system_prompt == _EXPECTED_KPI_DESIGNER_PROMPT


def test_evolution_framer_prompt_matches_original_wording() -> None:
    assert make_evolution_framer().system_prompt == _EXPECTED_EVOLUTION_FRAMER_PROMPT


def test_website_guide_prompt_matches_spec() -> None:
    assert make_website_guide().system_prompt == _EXPECTED_WEBSITE_GUIDE_PROMPT


def test_social_guide_prompt_matches_spec() -> None:
    assert make_social_guide().system_prompt == _EXPECTED_SOCIAL_GUIDE_PROMPT


def test_email_guide_prompt_matches_spec() -> None:
    assert make_email_guide().system_prompt == _EXPECTED_EMAIL_GUIDE_PROMPT


def test_events_guide_prompt_matches_spec() -> None:
    assert make_events_guide().system_prompt == _EXPECTED_EVENTS_GUIDE_PROMPT


def test_partnerships_guide_prompt_matches_spec() -> None:
    assert make_partnerships_guide().system_prompt == _EXPECTED_PARTNERSHIPS_GUIDE_PROMPT


def test_internal_guide_prompt_matches_spec() -> None:
    assert make_internal_guide().system_prompt == _EXPECTED_INTERNAL_GUIDE_PROMPT


def test_brand_in_action_illustrator_prompt_matches_spec() -> None:
    assert (
        make_brand_in_action_illustrator().system_prompt
        == _EXPECTED_BRAND_IN_ACTION_ILLUSTRATOR_PROMPT
    )


def test_training_planner_prompt_matches_spec() -> None:
    assert make_training_planner().system_prompt == _EXPECTED_TRAINING_PLANNER_PROMPT


def test_brand_rules_codifier_prompt_matches_spec() -> None:
    assert make_brand_rules_codifier().system_prompt == _EXPECTED_BRAND_RULES_CODIFIER_PROMPT
