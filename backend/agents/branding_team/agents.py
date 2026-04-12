"""Agent factory functions for the branding team Strands SDK pipeline.

Each function returns a configured ``strands.Agent`` instance for use as a
node in a ``GraphBuilder`` graph or ``Swarm``.  Agents are grouped by phase.

``BrandComplianceAgent`` is the only non-Strands class; it runs
outside the graph as a post-processing utility.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from strands import Agent

from .graphs.shared import build_agent
from .models import (
    BrandCheckRequest,
    BrandCheckResult,
    BrandingMission,
)

# ===================================================================
# Phase 1 — Strategic Core  (Graph: fan-out / fan-in)
# ===================================================================


def make_discovery_auditor() -> Agent:
    return build_agent(
        name="discovery_auditor",
        description="Analyses current brand perception, SWOT, and stakeholder insights.",
        system_prompt=(
            "You are a Brand Discovery Analyst. Given a branding mission, produce a comprehensive "
            "brand discovery audit. Include: current_brand_perception, market_position, strengths, "
            "weaknesses, opportunities, threats, and stakeholder_insights. Be specific and grounded "
            "in the company description and target audience provided. Output valid JSON matching the "
            "BrandDiscoveryAudit schema."
        ),
    )


def make_purpose_vision_writer() -> Agent:
    return build_agent(
        name="purpose_vision_writer",
        description="Crafts brand purpose, mission statement, and vision statement.",
        system_prompt=(
            "You are a Purpose & Vision Writer. Given a branding mission, write three things:\n"
            "1. brand_purpose — why the company exists (one sentence)\n"
            "2. mission_statement — what the company does for its audience (one sentence)\n"
            "3. vision_statement — the aspirational future state (one sentence)\n"
            "Be concise, inspiring, and specific to the company. Output valid JSON with these three keys."
        ),
    )


def make_values_articulator() -> Agent:
    return build_agent(
        name="values_articulator",
        description="Defines core values with behavioral definitions and observable behaviors.",
        system_prompt=(
            "You are a Values Articulator. Given a branding mission with optional seed values, "
            "produce a list of 3-5 core values. For each value provide:\n"
            "- value: the value name\n"
            "- behavioral_definition: what this value means in practice\n"
            "- observable_behaviors: 2-3 concrete behaviors that demonstrate this value\n"
            "Output valid JSON as a list of CoreValue objects."
        ),
    )


def make_audience_segmenter() -> Agent:
    return build_agent(
        name="audience_segmenter",
        description="Segments target audience with psychographic depth.",
        system_prompt=(
            "You are an Audience Segmenter. Given a branding mission, identify 1-3 target audience "
            "segments. For each segment provide: name, description, pain_points (2-3), goals (2-3), "
            "and decision_drivers (2-3). Ground your analysis in the company description and stated "
            "target audience. Output valid JSON as a list of AudienceSegment objects."
        ),
    )


def make_differentiation_mapper() -> Agent:
    return build_agent(
        name="differentiation_mapper",
        description="Maps competitive differentiation pillars with proof points.",
        system_prompt=(
            "You are a Differentiation Mapper. Given a branding mission with optional differentiators, "
            "produce 2-4 differentiation pillars. For each pillar provide:\n"
            "- pillar: the differentiator name\n"
            "- proof_points: 2-3 evidence items\n"
            "- competitive_context: how competitors fall short here\n"
            "Output valid JSON as a list of DifferentiationPillar objects."
        ),
    )


def make_positioning_synthesizer() -> Agent:
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
            "2. brand_promise — a one-sentence commitment to the customer\n"
            "Output valid JSON with these two keys."
        ),
    )


# ===================================================================
# Phase 2 — Narrative & Messaging  (Swarm)
# ===================================================================


def make_storyteller() -> Agent:
    return build_agent(
        name="Storyteller",
        description="Crafts the brand story, hero narrative, and boilerplate variants.",
        system_prompt=(
            "You are a Brand Storyteller. Using the strategic core output and branding mission, "
            "craft:\n"
            "1. brand_story — a compelling 2-3 paragraph origin/purpose story\n"
            "2. hero_narrative — a shorter, punchy version for hero sections\n"
            "3. boilerplate_variants — 3 versions (short/medium/long) for press and bios\n\n"
            "After completing your work, hand off to the ArchetypeAnalyst to define brand "
            "archetypes that align with the story. If the ArchetypeAnalyst suggests revisions, "
            "incorporate them."
        ),
    )


def make_archetype_analyst() -> Agent:
    return build_agent(
        name="ArchetypeAnalyst",
        description="Selects brand archetypes with rationale and personality traits.",
        system_prompt=(
            "You are a Brand Archetype Analyst. Review the brand story and strategic core, then "
            "select 1-2 brand archetypes (e.g. The Sage, The Creator, The Explorer). For each:\n"
            "- archetype: name\n"
            "- rationale: why this fits\n"
            "- personality_traits: 3-5 traits\n\n"
            "If the brand story doesn't align with the archetype, hand off back to the Storyteller "
            "with specific revision suggestions. Otherwise, hand off to the TaglineWriter."
        ),
    )


def make_tagline_writer() -> Agent:
    return build_agent(
        name="TaglineWriter",
        description="Creates tagline, tagline rationale, and elevator pitches.",
        system_prompt=(
            "You are a Tagline Writer. Using the brand story, archetypes, and strategic core, "
            "produce:\n"
            "1. tagline — a memorable brand tagline (max 8 words)\n"
            "2. tagline_rationale — why this tagline works\n"
            "3. elevator_pitches — three variants:\n"
            "   - tier: '5-second', pitch: ...\n"
            "   - tier: '30-second', pitch: ...\n"
            "   - tier: '2-minute', pitch: ...\n\n"
            "After completing, hand off to the MessageMapper."
        ),
    )


def make_message_mapper() -> Agent:
    return build_agent(
        name="MessageMapper",
        description="Builds messaging framework pillars and audience message maps.",
        system_prompt=(
            "You are a Message Mapper. Using all narrative context so far, produce:\n"
            "1. messaging_framework — 3-4 messaging pillars, each with:\n"
            "   - pillar, key_message, proof_points\n"
            "2. audience_message_maps — one per audience segment, each with:\n"
            "   - audience_segment, primary_message, supporting_messages, tone_adjustments\n\n"
            "After completing, hand off to the PersonaBuilder."
        ),
    )


def make_persona_builder() -> Agent:
    return build_agent(
        name="PersonaBuilder",
        description="Creates rich persona profiles with psychographic depth.",
        system_prompt=(
            "You are a Persona Builder. Using audience segments and brand narrative, create 2-3 "
            "persona profiles. Each persona has: name, role, demographics, psychographics, goals, "
            "frustrations, media_habits, jobs_to_be_done.\n\n"
            "After completing, hand off to the VoicePrinciplesDrafter."
        ),
    )


def make_voice_principles_drafter() -> Agent:
    return build_agent(
        name="VoicePrinciplesDrafter",
        description="Defines writing guidelines: voice principles, style dos/donts, editorial bar.",
        system_prompt=(
            "You are a Voice Principles Drafter. Using the brand story, archetypes, and mission's "
            "desired_voice, produce writing guidelines:\n"
            "1. voice_principles — 3-4 principles (e.g. 'Use a confident, human voice')\n"
            "2. style_dos — 3-4 writing best practices\n"
            "3. style_donts — 3-4 things to avoid\n"
            "4. editorial_quality_bar — 3-4 quality standards every piece must meet\n\n"
            "This is the final step in narrative development."
        ),
    )


# ===================================================================
# Phase 3 — Visual & Expressive Identity  (Graph-of-Swarm)
# ===================================================================

# --- Diverge Swarm agents ---


def make_creative_director() -> Agent:
    return build_agent(
        name="CreativeDirector",
        description="Coordinates moodboard ideation, dispatches conceptualists, reviews candidates.",
        system_prompt=(
            "You are a Creative Director leading visual identity exploration. Your job:\n"
            "1. Brief each MoodBoardConceptualist with a distinct visual direction\n"
            "2. Review their candidate moodboards\n"
            "3. Request revisions if needed\n"
            "4. Ensure at least 2-3 distinct candidates are produced\n\n"
            "Hand off to each conceptualist in turn. Once all candidates are collected, "
            "summarise the candidates and conclude."
        ),
    )


def make_moodboard_conceptualist(variant: str) -> Agent:
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
            f"- image_style: 3-4 image style descriptions\n\n"
            f"Hand back to the CreativeDirector when done."
        ),
    )


# --- Post-swarm Graph nodes ---


def make_converge_decider() -> Agent:
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
            "Output: winning_candidate_title, scoring_criteria, scores_by_candidate (dict of "
            "title→score), rationale, workshop_prompts (3 questions for stakeholders), and "
            "decision_criteria used."
        ),
    )


def make_logo_specifier() -> Agent:
    return build_agent(
        name="logo_specifier",
        description="Defines logo suite with usage rules.",
        system_prompt=(
            "You are a Logo Specifier. Based on the winning moodboard direction, define a logo "
            "suite. For each variant (primary, monochrome, icon-only, reversed), specify:\n"
            "- variant, usage_context, minimum_size, clear_space\n"
            "Output valid JSON as a list of LogoUsageRule objects."
        ),
    )


def make_color_system_builder() -> Agent:
    return build_agent(
        name="color_system_builder",
        description="Builds the brand color palette with psychological rationale.",
        system_prompt=(
            "You are a Color System Builder. Based on the winning moodboard direction, define "
            "5-7 colors. For each: name, hex_value, usage (where to use it), and "
            "psychological_rationale (why this color works for the brand). Include primary, "
            "secondary, accent, surface, and critical colors. Output valid JSON as a list of "
            "ColorEntry objects."
        ),
    )


def make_typography_builder() -> Agent:
    return build_agent(
        name="typography_builder",
        description="Defines the typography system.",
        system_prompt=(
            "You are a Typography Builder. Based on the winning moodboard direction, define a "
            "typography system with 3-4 type roles (display, body, caption, code). For each:\n"
            "- role, font_family, weight_range, usage_notes\n"
            "Output valid JSON as a list of TypographySpec objects."
        ),
    )


def make_iconography_director() -> Agent:
    return build_agent(
        name="iconography_director",
        description="Defines iconography and illustration style.",
        system_prompt=(
            "You are an Iconography Director. Based on the winning moodboard, define:\n"
            "1. iconography_style — describe the icon aesthetic (line weight, corner radius, fill)\n"
            "2. illustration_style — describe the illustration approach (flat, isometric, etc.)\n"
            "Output valid JSON with these two keys."
        ),
    )


def make_photography_video_director() -> Agent:
    return build_agent(
        name="photography_video_director",
        description="Defines photography direction, video direction, and motion principles.",
        system_prompt=(
            "You are a Photography & Video Director. Based on the winning moodboard, define:\n"
            "1. photography_direction — shooting style, lighting, composition, subjects\n"
            "2. video_direction — pacing, tone, visual style for video content\n"
            "3. motion_principles — 3-4 principles for animation/motion design\n"
            "Output valid JSON with these three keys."
        ),
    )


def make_voice_tone_builder() -> Agent:
    return build_agent(
        name="voice_tone_builder",
        description="Defines voice/tone spectrum and language dos/donts.",
        system_prompt=(
            "You are a Voice & Tone Builder. Using the brand narrative's writing guidelines and "
            "the moodboard direction, define:\n"
            "1. voice_tone_spectrum — for each context (marketing, support, legal, social, "
            "internal), specify the tone and 2-3 examples\n"
            "2. language_dos — 4-5 approved language patterns\n"
            "3. language_donts — 4-5 language anti-patterns\n"
            "Output valid JSON with these three keys."
        ),
    )


def make_design_system_codifier() -> Agent:
    return build_agent(
        name="design_system_codifier",
        description="Codifies the design system: principles, tokens, component standards.",
        system_prompt=(
            "You are a Design System Codifier. Based on the full visual identity work, produce:\n"
            "1. design_principles — 3-4 guiding principles (e.g. 'Clarity over decoration')\n"
            "2. foundation_tokens — 4-6 token categories (color, type, spacing, motion, etc.)\n"
            "3. component_standards — 3-5 component rules (buttons, cards, navigation, etc.)\n"
            "Output valid JSON matching the DesignSystemDefinition schema."
        ),
    )


# ===================================================================
# Phase 4 — Experience & Channel Activation  (Graph: fan-out / fan-in)
# ===================================================================


def make_brand_experience_principler() -> Agent:
    return build_agent(
        name="brand_experience_principler",
        description="Defines brand experience principles, signature moments, and sensory elements.",
        system_prompt=(
            "You are a Brand Experience Architect. Define:\n"
            "1. brand_experience_principles — 3-5 principles that govern every brand touchpoint\n"
            "2. signature_moments — 3-5 key moments in the customer journey that should feel "
            "distinctly on-brand\n"
            "3. sensory_elements — 2-4 sensory cues (sound, texture, scent, etc.) if applicable\n"
            "Output valid JSON with these three keys."
        ),
    )


def _make_channel_guide(channel: str, description: str) -> Agent:
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
            f"Context: {description}\n"
            f"Output valid JSON matching the ChannelGuideline schema."
        ),
    )


def make_website_guide() -> Agent:
    return _make_channel_guide("website", "Company website, landing pages, product pages.")


def make_social_guide() -> Agent:
    return _make_channel_guide("social", "Social media platforms (LinkedIn, Twitter, Instagram).")


def make_email_guide() -> Agent:
    return _make_channel_guide("email", "Email marketing, newsletters, transactional emails.")


def make_events_guide() -> Agent:
    return _make_channel_guide("events", "Conferences, webinars, meetups, trade shows.")


def make_partnerships_guide() -> Agent:
    return _make_channel_guide("partnerships", "Co-branding, sponsorships, partner marketing.")


def make_internal_guide() -> Agent:
    return _make_channel_guide("internal", "Internal comms, employee branding, onboarding.")


def make_brand_architecture_builder() -> Agent:
    return build_agent(
        name="brand_architecture_builder",
        description="Defines brand architecture rules, naming conventions, and terminology.",
        system_prompt=(
            "You are a Brand Architecture Specialist. Define:\n"
            "1. brand_architecture — rules for parent brand, sub-brands, product lines. Each "
            "with: entity, relationship, naming_convention, visual_treatment\n"
            "2. naming_conventions — 3-5 naming rules\n"
            "3. terminology_glossary — 5-10 key terms with definitions (dict)\n"
            "Output valid JSON with these three keys."
        ),
    )


def make_brand_in_action_illustrator() -> Agent:
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
            "Output valid JSON as a list of BrandInActionExample objects."
        ),
    )


# ===================================================================
# Phase 5 — Governance & Evolution  (Graph: fan-out / fan-in)
# ===================================================================


def make_ownership_definer() -> Agent:
    return build_agent(
        name="ownership_definer",
        description="Defines brand ownership model and decision authority matrix.",
        system_prompt=(
            "You are a Brand Ownership Definer. Define:\n"
            "1. ownership_model — who owns the brand (paragraph)\n"
            "2. decision_authority — a dict mapping decision types to responsible roles "
            "(e.g. 'logo_changes': 'Brand Director', 'campaign_messaging': 'Marketing Lead')\n"
            "Output valid JSON with these two keys."
        ),
    )


def make_approval_workflow_designer() -> Agent:
    return build_agent(
        name="approval_workflow_designer",
        description="Designs approval workflows and agency briefing protocols.",
        system_prompt=(
            "You are an Approval Workflow Designer. Define:\n"
            "1. approval_workflows — 3-5 workflows, each with: asset_type, approvers (list), "
            "sla, escalation_path\n"
            "2. agency_briefing_protocols — 3-5 protocols for briefing external agencies\n"
            "Output valid JSON with these two keys."
        ),
    )


def make_asset_wiki_planner() -> Agent:
    return build_agent(
        name="asset_wiki_planner",
        description="Plans asset management and brand wiki backlog.",
        system_prompt=(
            "You are an Asset & Wiki Planner. Define:\n"
            "1. asset_management_guidance — 3-5 guidelines for managing brand assets\n"
            "2. wiki_backlog — 4-6 wiki entries, each with: title, summary, owners (list), "
            "update_cadence. Cover: Brand North Star, Voice Playbook, Design System, Brand "
            "Review Intake, Channel Playbook, Governance Charter.\n"
            "Output valid JSON with these two keys."
        ),
    )


def make_training_planner() -> Agent:
    return build_agent(
        name="training_planner",
        description="Plans brand training and onboarding programmes.",
        system_prompt=(
            "You are a Training Planner. Define training_onboarding_plan — 4-6 training "
            "initiatives for onboarding new team members and maintaining brand literacy. "
            "Output valid JSON with a single key 'training_onboarding_plan' containing a list "
            "of strings."
        ),
    )


def make_kpi_designer() -> Agent:
    return build_agent(
        name="kpi_designer",
        description="Designs brand health KPIs with tracking methodology.",
        system_prompt=(
            "You are a Brand KPI Designer. Define:\n"
            "1. brand_health_kpis — 4-6 KPIs, each with: metric, measurement_method, target, "
            "review_frequency\n"
            "2. tracking_methodology — paragraph describing the measurement approach\n"
            "3. review_trigger_points — 3-5 events that should trigger a brand health review\n"
            "Output valid JSON with these three keys."
        ),
    )


def make_evolution_framer() -> Agent:
    return build_agent(
        name="evolution_framer",
        description="Defines the brand evolution framework and version control cadence.",
        system_prompt=(
            "You are a Brand Evolution Framer. Define:\n"
            "1. evolution_framework — paragraph describing how the brand evolves over time\n"
            "2. version_control_cadence — how often the brand system is formally reviewed "
            "and versioned\n"
            "Output valid JSON with these two keys."
        ),
    )


def make_brand_rules_codifier() -> Agent:
    return build_agent(
        name="brand_rules_codifier",
        description="Codifies top-level brand governance rules.",
        system_prompt=(
            "You are a Brand Rules Codifier. Using the full brand context (positioning, promise, "
            "values, narrative, visual identity), produce brand_guidelines — a list of 5-8 "
            "governance rules that everyone in the organisation must follow. Each rule is a "
            "single clear sentence. Cover: identity usage, messaging hierarchy, approval gates, "
            "asset management, and evolution. Output valid JSON with a single key "
            "'brand_guidelines' containing a list of strings."
        ),
    )


# ===================================================================
# Brand Compliance (outside the graph — post-processing utility)
# ===================================================================


@dataclass
class BrandComplianceAgent:
    """Evaluates whether assets are on-brand using keyword matching against mission values."""

    role: str = "Brand Compliance Reviewer"

    def evaluate(
        self, checks: List[BrandCheckRequest], mission: BrandingMission
    ) -> List[BrandCheckResult]:
        keywords = [
            *mission.values,
            *mission.differentiators,
            mission.company_name,
            mission.target_audience,
        ]
        lowered_keywords = [k.lower() for k in keywords if k]
        results: List[BrandCheckResult] = []

        for check in checks:
            text = f"{check.asset_name} {check.asset_description}".lower()
            matched = [k for k in lowered_keywords if k in text]
            is_on_brand = len(matched) >= 2
            confidence = min(0.95, 0.45 + (0.1 * len(matched)))

            rationale = [
                "Asset aligns with declared audience and brand language."
                if is_on_brand
                else "Asset is missing core brand signals.",
                f"Detected brand cues: {', '.join(matched[:4]) or 'none'}.",
            ]
            revision_suggestions = []
            if not is_on_brand:
                revision_suggestions = [
                    "Add clearer reference to target audience and expected outcome.",
                    "Use approved voice-and-tone language from the writing playbook.",
                    "Map copy to one narrative pillar and include proof.",
                ]

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
