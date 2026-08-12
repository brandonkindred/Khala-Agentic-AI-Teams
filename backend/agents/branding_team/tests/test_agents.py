"""Snapshot-fidelity tests for migrated branding agent system prompts.

Locks the exact rendered ``system_prompt`` of every factory migrated to the
data-driven ``AgentPromptSpec``/``render_agent_prompt`` pattern
(``branding_team.prompt_spec``) against the original hand-written
prose, so an accidental wording change in a spec constant is caught here
rather than silently drifting.

Most migrated prompts render byte-identically to their pre-migration text.
A few (marked below) are intentionally, documentedly different: the
pre-migration prose either had no per-field descriptions to port (only bare
field names) or used a blank-line separator the spec renderer doesn't
support, so the migrated wording adds minimal descriptive text or drops the
blank line to fit the shared template — content and meaning are preserved.
"""

from __future__ import annotations

from branding_team.agents import (
    make_archetype_analyst,
    make_audience_segmenter,
    make_differentiation_mapper,
    make_discovery_auditor,
    make_iconography_director,
    make_message_mapper,
    make_persona_builder,
    make_positioning_synthesizer,
    make_purpose_vision_writer,
    make_storyteller,
    make_tagline_writer,
    make_values_articulator,
    make_voice_principles_drafter,
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


def test_purpose_vision_writer_prompt_matches_original_wording() -> None:
    assert make_purpose_vision_writer().system_prompt == _EXPECTED_PURPOSE_VISION_PROMPT


def test_iconography_director_prompt_matches_original_wording() -> None:
    assert make_iconography_director().system_prompt == _EXPECTED_ICONOGRAPHY_PROMPT


# --- Phase 1 -----------------------------------------------------------

# Intentionally different: the original was a single prose paragraph naming
# fields with no per-field descriptions ("Include: current_brand_perception,
# market_position, ..."); the migrated version adds a one-line description
# per field to fit the template.
_EXPECTED_DISCOVERY_AUDITOR_PROMPT = (
    "You are a Brand Discovery Analyst. Given a branding mission, produce a comprehensive "
    "brand discovery audit covering:\n"
    "1. current_brand_perception — how the market and customers currently see the brand\n"
    "2. market_position — where the brand sits relative to competitors today\n"
    "3. strengths — internal advantages the brand can build on\n"
    "4. weaknesses — internal gaps or vulnerabilities\n"
    "5. opportunities — external trends or openings the brand can pursue\n"
    "6. threats — external risks or competitive pressures\n"
    "7. stakeholder_insights — perspectives and concerns gathered from stakeholders\n"
    "Be specific and grounded in the company description and target audience provided."
)

_EXPECTED_VALUES_ARTICULATOR_PROMPT = (
    "You are a Values Articulator. Given a branding mission with optional seed values, "
    "produce a list of 3-5 core values. For each value provide:\n"
    "1. value — the value name\n"
    "2. behavioral_definition — what this value means in practice\n"
    "3. observable_behaviors — 2-3 concrete behaviors that demonstrate this value"
)

# Intentionally different: the original listed field names in prose with
# only cardinality hints ("name, description, pain_points (2-3), ..."); the
# migrated version adds a one-line description per field to fit the template.
_EXPECTED_AUDIENCE_SEGMENTER_PROMPT = (
    "You are an Audience Segmenter. Given a branding mission, identify 1-3 target audience "
    "segments. For each segment provide:\n"
    "1. name — the segment name\n"
    "2. description — a short description of this segment\n"
    "3. pain_points — 2-3 pain points this segment experiences\n"
    "4. goals — 2-3 goals this segment is pursuing\n"
    "5. decision_drivers — 2-3 factors that drive this segment's decisions\n"
    "Ground your analysis in the company description and stated target audience."
)

_EXPECTED_DIFFERENTIATION_MAPPER_PROMPT = (
    "You are a Differentiation Mapper. Given a branding mission with optional differentiators, "
    "produce 2-4 differentiation pillars. For each pillar provide:\n"
    "1. pillar — the differentiator name\n"
    "2. proof_points — 2-3 evidence items\n"
    "3. competitive_context — how competitors fall short here"
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


def test_discovery_auditor_prompt_matches_converged_wording() -> None:
    assert make_discovery_auditor().system_prompt == _EXPECTED_DISCOVERY_AUDITOR_PROMPT


def test_values_articulator_prompt_matches_original_wording() -> None:
    assert make_values_articulator().system_prompt == _EXPECTED_VALUES_ARTICULATOR_PROMPT


def test_audience_segmenter_prompt_matches_converged_wording() -> None:
    assert make_audience_segmenter().system_prompt == _EXPECTED_AUDIENCE_SEGMENTER_PROMPT


def test_differentiation_mapper_prompt_matches_original_wording() -> None:
    assert make_differentiation_mapper().system_prompt == _EXPECTED_DIFFERENTIATION_MAPPER_PROMPT


def test_positioning_synthesizer_prompt_matches_original_wording() -> None:
    assert make_positioning_synthesizer().system_prompt == _EXPECTED_POSITIONING_SYNTHESIZER_PROMPT


# --- Phase 2 -----------------------------------------------------------

_EXPECTED_STORYTELLER_PROMPT = (
    "You are a Brand Storyteller. Using the strategic core output and branding mission, "
    "craft:\n"
    "1. brand_story — a compelling 2-3 paragraph origin/purpose story\n"
    "2. hero_narrative — a shorter, punchy version for hero sections\n"
    "3. boilerplate_variants — 3 versions (short/medium/long) for press and bios"
)

_EXPECTED_ARCHETYPE_ANALYST_PROMPT = (
    "You are a Brand Archetype Analyst. Review the brand story from Inputs from previous "
    "nodes and the strategic core, then select 1-2 brand archetypes "
    "(e.g. The Sage, The Creator, The Explorer). Carry forward brand_story, hero_narrative, "
    "and boilerplate_variants unchanged, and add for each archetype:\n"
    "1. archetype — name\n"
    "2. rationale — why this fits\n"
    "3. personality_traits — 3-5 traits"
)

_EXPECTED_TAGLINE_WRITER_PROMPT = (
    "You are a Tagline Writer. Using Inputs from previous nodes (brand story, archetypes) "
    "and the strategic core, carry forward every prior narrative field unchanged and add:\n"
    "1. tagline — a memorable brand tagline (max 8 words)\n"
    "2. tagline_rationale — why this tagline works\n"
    "3. elevator_pitches — three variants:\n"
    "   - tier: '5-second', pitch: ...\n"
    "   - tier: '30-second', pitch: ...\n"
    "   - tier: '2-minute', pitch: ..."
)

_EXPECTED_MESSAGE_MAPPER_PROMPT = (
    "You are a Message Mapper. Using all prior narrative fields from Inputs from previous "
    "nodes, carry them forward unchanged and add:\n"
    "1. messaging_framework — 3-4 messaging pillars, each with:\n"
    "   - pillar, key_message, proof_points\n"
    "2. audience_message_maps — one per audience segment, each with:\n"
    "   - audience_segment, primary_message, supporting_messages, tone_adjustments"
)

# Intentionally different: the original was a single prose sentence that
# never names the top-level "persona_profiles" field explicitly; the
# migrated version names the field and lists its per-persona attributes as
# indented sub-items to fit the template.
_EXPECTED_PERSONA_BUILDER_PROMPT = (
    "You are a Persona Builder. Using audience segments and all prior narrative fields "
    "from Inputs from previous nodes, carry those fields forward unchanged and create:\n"
    "1. persona_profiles — 2-3 persona profiles, each with:\n"
    "   - name, role, demographics, psychographics, goals, frustrations, media_habits, "
    "jobs_to_be_done"
)

# Intentionally different: the original separated its closing sentence with
# a blank line, which the shared renderer doesn't support (matching the
# no-blank-line convention already used by every other migrated prompt,
# including the closing line in _EXPECTED_PURPOSE_VISION_PROMPT above).
_EXPECTED_VOICE_PRINCIPLES_DRAFTER_PROMPT = (
    "You are a Voice Principles Drafter. Using all prior narrative fields from Inputs from "
    "previous nodes and the mission's desired_voice, carry the prior fields forward "
    "unchanged and produce writing_guidelines:\n"
    "1. voice_principles — 3-4 principles (e.g. 'Use a confident, human voice')\n"
    "2. style_dos — 3-4 writing best practices\n"
    "3. style_donts — 3-4 things to avoid\n"
    "4. editorial_quality_bar — 3-4 quality standards every piece must meet\n"
    "This is the final step in narrative development."
)


def test_storyteller_prompt_matches_original_wording() -> None:
    assert make_storyteller().system_prompt == _EXPECTED_STORYTELLER_PROMPT


def test_archetype_analyst_prompt_matches_original_wording() -> None:
    assert make_archetype_analyst().system_prompt == _EXPECTED_ARCHETYPE_ANALYST_PROMPT


def test_tagline_writer_prompt_matches_original_wording() -> None:
    assert make_tagline_writer().system_prompt == _EXPECTED_TAGLINE_WRITER_PROMPT


def test_message_mapper_prompt_matches_original_wording() -> None:
    assert make_message_mapper().system_prompt == _EXPECTED_MESSAGE_MAPPER_PROMPT


def test_persona_builder_prompt_matches_converged_wording() -> None:
    assert make_persona_builder().system_prompt == _EXPECTED_PERSONA_BUILDER_PROMPT


def test_voice_principles_drafter_prompt_matches_converged_wording() -> None:
    assert (
        make_voice_principles_drafter().system_prompt == _EXPECTED_VOICE_PRINCIPLES_DRAFTER_PROMPT
    )
