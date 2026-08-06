"""Validation tests for the Phase 1, Phase 2, and Phase 4 structured-output wrapper models.

These agent-facing models (``BrandDiscoveryAuditOutput``, ``PurposeVisionOutput``,
``CoreValuesOutput``, ``AudienceSegmentsOutput``, ``DifferentiationPillarsOutput``,
``PositioningOutput``, plus Phase 2's ``BrandStoryOutput``,
``BrandArchetypesOutput``, ``TaglineOutput``, ``MessagingFrameworkOutput``
(and its nested ``MessagingPillarOutput``/``AudienceMessageMapOutput``),
``PersonaProfilesOutput``, ``WritingGuidelinesOutput``, plus Phase 4's
``ChannelGuidelineOutput``, ``BrandArchitectureOutput``, and
``BrandExperiencePrinciplesOutput``) must reject empty/omitted content so
Strands' structured-output tool retries the LLM
instead of silently accepting a blank or under-cardinality response (see
``structured_output_tool.py``: a ``ValidationError`` becomes a tool error
the model is asked to fix).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from branding_team.models import (
    AudienceMessageMapOutput,
    AudienceSegmentOutput,
    AudienceSegmentsOutput,
    BrandArchetypeOutput,
    BrandArchetypesOutput,
    BrandArchitectureOutput,
    BrandArchitectureRuleOutput,
    BrandDiscoveryAuditOutput,
    BrandExperiencePrinciplesOutput,
    BrandStoryOutput,
    ChannelGuidelineOutput,
    CoreValueOutput,
    CoreValuesOutput,
    DifferentiationPillarOutput,
    DifferentiationPillarsOutput,
    ElevatorPitchOutput,
    MessagingFrameworkOutput,
    MessagingPillarOutput,
    PersonaProfileOutput,
    PersonaProfilesOutput,
    PositioningOutput,
    PurposeVisionOutput,
    TaglineOutput,
    WritingGuidelinesBody,
    WritingGuidelinesOutput,
)

_DISCOVERY_KWARGS = dict(
    current_brand_perception="Seen as reliable but generic.",
    market_position="Mid-market challenger.",
    strengths=["Delivery speed"],
    weaknesses=["Low brand recall"],
    opportunities=["Category consolidating"],
    threats=["Bigger competitors out-spending"],
    stakeholder_insights=["Sales wants sharper differentiation"],
)


def test_brand_discovery_audit_output_rejects_missing_and_empty_fields() -> None:
    with pytest.raises(ValidationError):
        BrandDiscoveryAuditOutput()
    with pytest.raises(ValidationError):
        BrandDiscoveryAuditOutput(**{**_DISCOVERY_KWARGS, "current_brand_perception": ""})
    with pytest.raises(ValidationError):
        BrandDiscoveryAuditOutput(**{**_DISCOVERY_KWARGS, "strengths": []})

    output = BrandDiscoveryAuditOutput(**_DISCOVERY_KWARGS)
    assert output.market_position == "Mid-market challenger."


def test_brand_discovery_audit_output_rejects_blank_list_items() -> None:
    """Container-level min_length isn't enough — blank items must fail too."""
    with pytest.raises(ValidationError):
        BrandDiscoveryAuditOutput(**{**_DISCOVERY_KWARGS, "strengths": [""]})
    with pytest.raises(ValidationError):
        BrandDiscoveryAuditOutput(**{**_DISCOVERY_KWARGS, "weaknesses": [""]})
    with pytest.raises(ValidationError):
        BrandDiscoveryAuditOutput(**{**_DISCOVERY_KWARGS, "opportunities": [""]})
    with pytest.raises(ValidationError):
        BrandDiscoveryAuditOutput(**{**_DISCOVERY_KWARGS, "threats": [""]})
    with pytest.raises(ValidationError):
        BrandDiscoveryAuditOutput(**{**_DISCOVERY_KWARGS, "stakeholder_insights": [""]})


def test_purpose_vision_output_rejects_missing_and_empty_fields() -> None:
    with pytest.raises(ValidationError):
        PurposeVisionOutput()
    with pytest.raises(ValidationError):
        PurposeVisionOutput(brand_purpose="", mission_statement="x", vision_statement="x")

    output = PurposeVisionOutput(brand_purpose="a", mission_statement="b", vision_statement="c")
    assert output.brand_purpose == "a"


def test_positioning_output_rejects_missing_and_empty_fields() -> None:
    with pytest.raises(ValidationError):
        PositioningOutput()
    with pytest.raises(ValidationError):
        PositioningOutput(positioning_statement="", brand_promise="x")

    output = PositioningOutput(positioning_statement="x", brand_promise="y")
    assert output.brand_promise == "y"


def test_core_values_output_enforces_stated_cardinality() -> None:
    """Prompt asks for "3-5 core values"."""
    value = CoreValueOutput(
        value="clarity",
        behavioral_definition="We demonstrate clarity in every decision.",
        observable_behaviors=["Plain-language docs"],
    )
    with pytest.raises(ValidationError):
        CoreValuesOutput(core_values=[value, value])  # below min of 3
    with pytest.raises(ValidationError):
        CoreValuesOutput(core_values=[value] * 6)  # above max of 5

    output = CoreValuesOutput(core_values=[value] * 3)
    assert len(output.core_values) == 3


def test_core_value_output_rejects_blank_content() -> None:
    """A blank value, behavioral definition, or observable behavior must fail validation."""
    valid_kwargs = dict(
        value="clarity",
        behavioral_definition="We demonstrate clarity in every decision.",
        observable_behaviors=["Plain-language docs"],
    )

    with pytest.raises(ValidationError):
        CoreValueOutput(**{**valid_kwargs, "value": ""})
    with pytest.raises(ValidationError):
        CoreValueOutput(**{**valid_kwargs, "behavioral_definition": ""})
    with pytest.raises(ValidationError):
        CoreValueOutput(**{**valid_kwargs, "observable_behaviors": [""]})
    with pytest.raises(ValidationError):
        CoreValueOutput(**{**valid_kwargs, "observable_behaviors": []})

    output = CoreValueOutput(**valid_kwargs)
    assert output.value == "clarity"


def test_audience_segments_output_enforces_stated_cardinality() -> None:
    """Prompt asks for "1-3 target audience segments"."""
    segment = AudienceSegmentOutput(
        name="Enterprise leaders",
        description="VP/Director-level buyers at mid-market SaaS companies.",
        pain_points=["Inconsistent brand touchpoints"],
        goals=["Ship cohesive experiences"],
        decision_drivers=["Proven execution speed"],
    )
    with pytest.raises(ValidationError):
        AudienceSegmentsOutput(target_audience_segments=[])  # below min of 1
    with pytest.raises(ValidationError):
        AudienceSegmentsOutput(target_audience_segments=[segment] * 4)  # above max of 3

    output = AudienceSegmentsOutput(target_audience_segments=[segment])
    assert len(output.target_audience_segments) == 1


def test_audience_segment_output_rejects_blank_content() -> None:
    """A blank name, description, or list item must fail validation."""
    valid_kwargs = dict(
        name="Enterprise leaders",
        description="VP/Director-level buyers at mid-market SaaS companies.",
        pain_points=["Inconsistent brand touchpoints"],
        goals=["Ship cohesive experiences"],
        decision_drivers=["Proven execution speed"],
    )

    with pytest.raises(ValidationError):
        AudienceSegmentOutput(**{**valid_kwargs, "name": ""})
    with pytest.raises(ValidationError):
        AudienceSegmentOutput(**{**valid_kwargs, "description": ""})
    with pytest.raises(ValidationError):
        AudienceSegmentOutput(**{**valid_kwargs, "pain_points": [""]})
    with pytest.raises(ValidationError):
        AudienceSegmentOutput(**{**valid_kwargs, "goals": []})
    with pytest.raises(ValidationError):
        AudienceSegmentOutput(**{**valid_kwargs, "decision_drivers": [""]})

    output = AudienceSegmentOutput(**valid_kwargs)
    assert output.name == "Enterprise leaders"


def test_differentiation_pillars_output_enforces_stated_cardinality() -> None:
    """Prompt asks for "2-4 differentiation pillars"."""
    pillar = DifferentiationPillarOutput(
        pillar="Execution speed",
        proof_points=["Ship weekly release cadence"],
        competitive_context="Competitors ship quarterly.",
    )
    with pytest.raises(ValidationError):
        DifferentiationPillarsOutput(differentiation_pillars=[pillar])  # below min of 2
    with pytest.raises(ValidationError):
        DifferentiationPillarsOutput(differentiation_pillars=[pillar] * 5)  # above max of 4

    output = DifferentiationPillarsOutput(differentiation_pillars=[pillar] * 2)
    assert len(output.differentiation_pillars) == 2


def test_differentiation_pillar_output_rejects_blank_content() -> None:
    """A blank pillar, competitive context, or proof point must fail validation."""
    valid_kwargs = dict(
        pillar="Execution speed",
        proof_points=["Ship weekly release cadence"],
        competitive_context="Competitors ship quarterly.",
    )

    with pytest.raises(ValidationError):
        DifferentiationPillarOutput(**{**valid_kwargs, "pillar": ""})
    with pytest.raises(ValidationError):
        DifferentiationPillarOutput(**{**valid_kwargs, "competitive_context": ""})
    with pytest.raises(ValidationError):
        DifferentiationPillarOutput(**{**valid_kwargs, "proof_points": [""]})
    with pytest.raises(ValidationError):
        DifferentiationPillarOutput(**{**valid_kwargs, "proof_points": []})

    output = DifferentiationPillarOutput(**valid_kwargs)
    assert output.pillar == "Execution speed"


def test_brand_story_output_rejects_missing_and_enforces_cardinality() -> None:
    """Prompt asks for 3 boilerplate variants (short/medium/long)."""
    with pytest.raises(ValidationError):
        BrandStoryOutput()
    with pytest.raises(ValidationError):
        BrandStoryOutput(brand_story="", hero_narrative="x", boilerplate_variants=["a", "b", "c"])
    with pytest.raises(ValidationError):
        BrandStoryOutput(brand_story="a", hero_narrative="b", boilerplate_variants=["a", "b"])

    output = BrandStoryOutput(
        brand_story="Origin story.",
        hero_narrative="Punchy hero.",
        boilerplate_variants=["short", "medium", "long"],
    )
    assert output.hero_narrative == "Punchy hero."


def test_brand_story_output_rejects_blank_boilerplate_variant() -> None:
    """A blank short/medium/long variant must fail validation, not just wrong count."""
    with pytest.raises(ValidationError):
        BrandStoryOutput(
            brand_story="Origin story.",
            hero_narrative="Punchy hero.",
            boilerplate_variants=["", "medium", "long"],
        )
    with pytest.raises(ValidationError):
        BrandStoryOutput(
            brand_story="Origin story.",
            hero_narrative="Punchy hero.",
            boilerplate_variants=["short", "", "long"],
        )
    with pytest.raises(ValidationError):
        BrandStoryOutput(
            brand_story="Origin story.",
            hero_narrative="Punchy hero.",
            boilerplate_variants=["short", "medium", ""],
        )


_STORY = dict(
    brand_story="Origin story.",
    hero_narrative="Punchy hero.",
    boilerplate_variants=["short", "medium", "long"],
)
_ARCHETYPE = BrandArchetypeOutput(
    archetype="The Creator", rationale="Inventive.", personality_traits=["Imaginative", "Original"]
)
_PITCHES = [
    ElevatorPitchOutput(tier="5-second", pitch="a"),
    ElevatorPitchOutput(tier="30-second", pitch="b"),
    ElevatorPitchOutput(tier="2-minute", pitch="c"),
]
_PILLAR = MessagingPillarOutput(
    pillar="Cohesion", key_message="One voice everywhere.", proof_points=["Style guide"]
)
_AUDIENCE = AudienceMessageMapOutput(
    audience_segment="Enterprise leaders",
    primary_message="Ship on-brand, faster.",
    supporting_messages=["Consistent across every touchpoint"],
    tone_adjustments="Confident, outcome-focused",
)
_PERSONA = PersonaProfileOutput(
    name="Alex",
    role="Product Lead",
    demographics="30-40, urban",
    psychographics="Pragmatic, values clarity",
    goals=["Ship on brand"],
    frustrations=["Inconsistent guidelines"],
    media_habits=["Trade newsletters"],
    jobs_to_be_done=["Brief the design team"],
)
_GUIDELINES = WritingGuidelinesBody(
    voice_principles=["a", "b", "c"],
    style_dos=["a", "b", "c"],
    style_donts=["a", "b", "c"],
    editorial_quality_bar=["a", "b", "c"],
)


def test_brand_archetypes_output_enforces_stated_cardinality() -> None:
    """Prompt asks for "1-2 brand archetypes"; inherits Storyteller fields."""
    with pytest.raises(ValidationError):
        BrandArchetypesOutput(**_STORY, brand_archetypes=[])
    with pytest.raises(ValidationError):
        BrandArchetypesOutput(**_STORY, brand_archetypes=[_ARCHETYPE] * 3)

    output = BrandArchetypesOutput(**_STORY, brand_archetypes=[_ARCHETYPE])
    assert len(output.brand_archetypes) == 1
    assert output.brand_story == "Origin story."


def test_brand_archetype_output_rejects_blank_content() -> None:
    """A blank archetype, rationale, or personality trait must fail validation."""
    valid_kwargs = _ARCHETYPE.model_dump()

    with pytest.raises(ValidationError):
        BrandArchetypeOutput(**{**valid_kwargs, "archetype": ""})
    with pytest.raises(ValidationError):
        BrandArchetypeOutput(**{**valid_kwargs, "rationale": ""})
    with pytest.raises(ValidationError):
        BrandArchetypeOutput(**{**valid_kwargs, "personality_traits": [""]})
    with pytest.raises(ValidationError):
        BrandArchetypeOutput(**{**valid_kwargs, "personality_traits": []})

    output = BrandArchetypeOutput(**valid_kwargs)
    assert output.archetype == "The Creator"


def test_tagline_output_rejects_missing_and_enforces_cardinality() -> None:
    """Prompt asks for three elevator pitch tiers; inherits prior narrative."""
    base = {**_STORY, "brand_archetypes": [_ARCHETYPE]}
    with pytest.raises(ValidationError):
        TaglineOutput(**base, tagline="", tagline_rationale="x", elevator_pitches=_PITCHES)
    with pytest.raises(ValidationError):
        TaglineOutput(**base, tagline="x", tagline_rationale="y", elevator_pitches=_PITCHES[:2])

    output = TaglineOutput(
        **base, tagline="Ship brand", tagline_rationale="Clear", elevator_pitches=_PITCHES
    )
    assert output.tagline == "Ship brand"
    assert output.brand_archetypes[0].archetype == "The Creator"


def test_tagline_output_rejects_blank_elevator_pitch_fields() -> None:
    """A blank tier or pitch in any of the three elevator pitches must fail validation."""
    base = {
        **_STORY,
        "brand_archetypes": [_ARCHETYPE],
        "tagline": "Ship brand",
        "tagline_rationale": "Clear",
    }
    blank_tier = [{"tier": "", "pitch": "a"}, _PITCHES[1], _PITCHES[2]]
    blank_pitch = [{"tier": "5-second", "pitch": ""}, _PITCHES[1], _PITCHES[2]]

    with pytest.raises(ValidationError):
        TaglineOutput(**base, elevator_pitches=blank_tier)
    with pytest.raises(ValidationError):
        TaglineOutput(**base, elevator_pitches=blank_pitch)

    output = TaglineOutput(**base, elevator_pitches=_PITCHES)
    assert output.elevator_pitches[0].tier == "5-second"


def test_messaging_framework_output_enforces_stated_cardinality() -> None:
    """Prompt asks for "3-4 messaging pillars" and at least one audience map."""
    base = {
        **_STORY,
        "brand_archetypes": [_ARCHETYPE],
        "tagline": "Ship brand",
        "tagline_rationale": "Clear",
        "elevator_pitches": _PITCHES,
    }
    with pytest.raises(ValidationError):
        MessagingFrameworkOutput(
            **base, messaging_framework=[_PILLAR, _PILLAR], audience_message_maps=[_AUDIENCE]
        )
    with pytest.raises(ValidationError):
        MessagingFrameworkOutput(
            **base, messaging_framework=[_PILLAR] * 5, audience_message_maps=[_AUDIENCE]
        )
    with pytest.raises(ValidationError):
        MessagingFrameworkOutput(
            **base, messaging_framework=[_PILLAR] * 3, audience_message_maps=[]
        )

    output = MessagingFrameworkOutput(
        **base, messaging_framework=[_PILLAR] * 3, audience_message_maps=[_AUDIENCE]
    )
    assert len(output.messaging_framework) == 3


def test_messaging_pillar_and_audience_map_outputs_reject_blank_content() -> None:
    """A blank pillar or audience segment must fail validation, not silently pass."""
    valid_pillar_kwargs = _PILLAR.model_dump()
    valid_audience_kwargs = _AUDIENCE.model_dump()

    with pytest.raises(ValidationError):
        MessagingPillarOutput(**{**valid_pillar_kwargs, "pillar": ""})
    with pytest.raises(ValidationError):
        MessagingPillarOutput(**{**valid_pillar_kwargs, "key_message": ""})
    with pytest.raises(ValidationError):
        MessagingPillarOutput(**{**valid_pillar_kwargs, "proof_points": [""]})

    with pytest.raises(ValidationError):
        AudienceMessageMapOutput(**{**valid_audience_kwargs, "audience_segment": ""})
    with pytest.raises(ValidationError):
        AudienceMessageMapOutput(**{**valid_audience_kwargs, "primary_message": ""})
    with pytest.raises(ValidationError):
        AudienceMessageMapOutput(**{**valid_audience_kwargs, "supporting_messages": [""]})

    pillar = MessagingPillarOutput(**valid_pillar_kwargs)
    audience = AudienceMessageMapOutput(**valid_audience_kwargs)
    assert pillar.pillar == "Cohesion"
    assert audience.audience_segment == "Enterprise leaders"


def test_persona_profiles_output_enforces_stated_cardinality() -> None:
    """Prompt asks for "2-3 persona profiles"."""
    base = {
        **_STORY,
        "brand_archetypes": [_ARCHETYPE],
        "tagline": "Ship brand",
        "tagline_rationale": "Clear",
        "elevator_pitches": _PITCHES,
        "messaging_framework": [_PILLAR] * 3,
        "audience_message_maps": [_AUDIENCE],
    }
    with pytest.raises(ValidationError):
        PersonaProfilesOutput(**base, persona_profiles=[_PERSONA])
    with pytest.raises(ValidationError):
        PersonaProfilesOutput(**base, persona_profiles=[_PERSONA] * 4)

    output = PersonaProfilesOutput(**base, persona_profiles=[_PERSONA, _PERSONA])
    assert len(output.persona_profiles) == 2


def test_persona_profile_output_rejects_blank_name() -> None:
    """A blank-name persona must fail validation, not silently produce empty output."""
    valid_kwargs = _PERSONA.model_dump()

    with pytest.raises(ValidationError):
        PersonaProfileOutput(**{**valid_kwargs, "name": ""})
    with pytest.raises(ValidationError):
        PersonaProfileOutput(**{**valid_kwargs, "role": ""})
    with pytest.raises(ValidationError):
        PersonaProfileOutput(**{**valid_kwargs, "goals": [""]})

    output = PersonaProfileOutput(**valid_kwargs)
    assert output.name == "Alex"


def test_writing_guidelines_output_rejects_missing_and_enforces_cardinality() -> None:
    """Voice agent requires full carry-forward plus nested writing_guidelines."""
    base = {
        **_STORY,
        "brand_archetypes": [_ARCHETYPE],
        "tagline": "Ship brand",
        "tagline_rationale": "Clear",
        "elevator_pitches": _PITCHES,
        "messaging_framework": [_PILLAR] * 3,
        "audience_message_maps": [_AUDIENCE],
        "persona_profiles": [_PERSONA, _PERSONA],
    }
    with pytest.raises(ValidationError):
        WritingGuidelinesOutput(**base)
    with pytest.raises(ValidationError):
        WritingGuidelinesOutput(
            **base,
            writing_guidelines=WritingGuidelinesBody(
                voice_principles=["a", "b"],
                style_dos=["a", "b", "c"],
                style_donts=["a", "b", "c"],
                editorial_quality_bar=["a", "b", "c"],
            ),
        )

    output = WritingGuidelinesOutput(**base, writing_guidelines=_GUIDELINES)
    assert len(output.writing_guidelines.voice_principles) == 3


def test_writing_guidelines_body_rejects_blank_list_items() -> None:
    """``voice_principles``/``style_dos``/``style_donts``/``editorial_quality_bar`` reject blanks."""
    output = WritingGuidelinesBody(
        voice_principles=["a", "b", "c"],
        style_dos=["a", "b", "c"],
        style_donts=["a", "b", "c"],
        editorial_quality_bar=["a", "b", "c"],
    )
    assert len(output.voice_principles) == 3

    valid = dict(
        voice_principles=["a", "b", "c"],
        style_dos=["a", "b", "c"],
        style_donts=["a", "b", "c"],
        editorial_quality_bar=["a", "b", "c"],
    )
    with pytest.raises(ValidationError):
        WritingGuidelinesBody(**{**valid, "voice_principles": ["", "", ""]})
    with pytest.raises(ValidationError):
        WritingGuidelinesBody(**{**valid, "style_dos": ["", "", ""]})
    with pytest.raises(ValidationError):
        WritingGuidelinesBody(**{**valid, "style_donts": ["", "", ""]})
    with pytest.raises(ValidationError):
        WritingGuidelinesBody(**{**valid, "editorial_quality_bar": ["", "", ""]})


_CHANNEL_GUIDE_KWARGS = dict(
    channel="website",
    strategy="Lead with product proof points.",
    dos=["Use active voice", "Lead with benefits", "Link to case studies"],
    donts=["Bury the CTA", "Overuse jargon", "Ignore mobile layout"],
    content_types=["Landing pages", "Case studies", "Product demos"],
    frequency_guidance="Refresh quarterly.",
)


def test_channel_guideline_output_rejects_blank_list_items() -> None:
    """``dos``/``donts``/``content_types`` must reject blank items, not just wrong counts."""
    output = ChannelGuidelineOutput(**_CHANNEL_GUIDE_KWARGS)
    assert len(output.dos) == 3

    with pytest.raises(ValidationError):
        ChannelGuidelineOutput(**{**_CHANNEL_GUIDE_KWARGS, "dos": ["", "", ""]})
    with pytest.raises(ValidationError):
        ChannelGuidelineOutput(**{**_CHANNEL_GUIDE_KWARGS, "donts": ["", "", ""]})
    with pytest.raises(ValidationError):
        ChannelGuidelineOutput(**{**_CHANNEL_GUIDE_KWARGS, "content_types": ["", "", ""]})


_ARCHITECTURE_RULE = BrandArchitectureRuleOutput(
    entity="Parent brand",
    relationship="Umbrella over sub-brands",
    naming_convention="[Parent] [Product]",
    visual_treatment="Shared wordmark, distinct accent color",
)
_ARCHITECTURE_KWARGS = dict(
    brand_architecture=[_ARCHITECTURE_RULE],
    naming_conventions=["Title Case", "No abbreviations", "Product before feature"],
    terminology_glossary={
        "Sub-brand": "A product line under the parent brand",
        "Wordmark": "The brand's logotype",
        "Accent color": "Secondary color reserved for sub-brand distinction",
        "Umbrella brand": "The parent brand covering all sub-brands",
        "Naming convention": "The pattern used to name new products",
    },
)


def test_brand_architecture_output_rejects_blank_naming_conventions_and_glossary_entries() -> None:
    """``naming_conventions`` items and glossary keys/values must reject blank content."""
    output = BrandArchitectureOutput(**_ARCHITECTURE_KWARGS)
    assert len(output.naming_conventions) == 3

    with pytest.raises(ValidationError):
        BrandArchitectureOutput(**{**_ARCHITECTURE_KWARGS, "naming_conventions": ["", "", ""]})
    with pytest.raises(ValidationError):
        BrandArchitectureOutput(
            **{
                **_ARCHITECTURE_KWARGS,
                "terminology_glossary": {
                    **_ARCHITECTURE_KWARGS["terminology_glossary"],
                    "": "blank key",
                },
            }
        )
    with pytest.raises(ValidationError):
        BrandArchitectureOutput(
            **{
                **_ARCHITECTURE_KWARGS,
                "terminology_glossary": {
                    **_ARCHITECTURE_KWARGS["terminology_glossary"],
                    "Blank value": "",
                },
            }
        )


_EXPERIENCE_PRINCIPLES_KWARGS = dict(
    brand_experience_principles=["Consistency", "Intentionality", "Warmth"],
    signature_moments=["First visit", "Onboarding", "Renewal"],
    sensory_elements=["Signature sound", "Motion easing"],
)


def test_brand_experience_principles_output_rejects_blank_list_items() -> None:
    """``brand_experience_principles``/``signature_moments``/``sensory_elements`` reject blanks."""
    output = BrandExperiencePrinciplesOutput(**_EXPERIENCE_PRINCIPLES_KWARGS)
    assert len(output.brand_experience_principles) == 3

    with pytest.raises(ValidationError):
        BrandExperiencePrinciplesOutput(
            **{**_EXPERIENCE_PRINCIPLES_KWARGS, "brand_experience_principles": ["", "", ""]}
        )
    with pytest.raises(ValidationError):
        BrandExperiencePrinciplesOutput(
            **{**_EXPERIENCE_PRINCIPLES_KWARGS, "signature_moments": ["", "", ""]}
        )
    with pytest.raises(ValidationError):
        BrandExperiencePrinciplesOutput(
            **{**_EXPERIENCE_PRINCIPLES_KWARGS, "sensory_elements": ["", ""]}
        )
