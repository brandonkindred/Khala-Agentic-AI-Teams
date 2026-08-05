"""Validation tests for the Phase 1, Phase 2, and Phase 4 structured-output wrapper models.

These agent-facing models (``BrandDiscoveryAuditOutput``, ``PurposeVisionOutput``,
``CoreValuesOutput``, ``AudienceSegmentsOutput``, ``DifferentiationPillarsOutput``,
``PositioningOutput``, plus Phase 2's ``BrandStoryOutput``,
``BrandArchetypesOutput``, ``TaglineOutput``, ``MessagingFrameworkOutput``,
``PersonaProfilesOutput``, ``WritingGuidelinesOutput``, plus Phase 4's
``ChannelGuidelineOutput`` and ``BrandArchitectureOutput``) must reject
empty/omitted content so Strands' structured-output tool retries the LLM
instead of silently accepting a blank or under-cardinality response (see
``structured_output_tool.py``: a ``ValidationError`` becomes a tool error
the model is asked to fix).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from branding_team.models import (
    AudienceMessageMap,
    AudienceSegment,
    AudienceSegmentsOutput,
    BrandArchetype,
    BrandArchetypesOutput,
    BrandArchitectureOutput,
    BrandArchitectureRuleOutput,
    BrandDiscoveryAuditOutput,
    BrandStoryOutput,
    ChannelGuidelineOutput,
    CoreValue,
    CoreValuesOutput,
    DifferentiationPillar,
    DifferentiationPillarsOutput,
    ElevatorPitch,
    MessagingFrameworkOutput,
    MessagingPillar,
    PersonaProfile,
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
    value = CoreValue(value="clarity")
    with pytest.raises(ValidationError):
        CoreValuesOutput(core_values=[value, value])  # below min of 3
    with pytest.raises(ValidationError):
        CoreValuesOutput(core_values=[value] * 6)  # above max of 5

    output = CoreValuesOutput(core_values=[value] * 3)
    assert len(output.core_values) == 3


def test_audience_segments_output_enforces_stated_cardinality() -> None:
    """Prompt asks for "1-3 target audience segments"."""
    segment = AudienceSegment(name="Enterprise leaders")
    with pytest.raises(ValidationError):
        AudienceSegmentsOutput(target_audience_segments=[])  # below min of 1
    with pytest.raises(ValidationError):
        AudienceSegmentsOutput(target_audience_segments=[segment] * 4)  # above max of 3

    output = AudienceSegmentsOutput(target_audience_segments=[segment])
    assert len(output.target_audience_segments) == 1


def test_differentiation_pillars_output_enforces_stated_cardinality() -> None:
    """Prompt asks for "2-4 differentiation pillars"."""
    pillar = DifferentiationPillar(pillar="Execution speed")
    with pytest.raises(ValidationError):
        DifferentiationPillarsOutput(differentiation_pillars=[pillar])  # below min of 2
    with pytest.raises(ValidationError):
        DifferentiationPillarsOutput(differentiation_pillars=[pillar] * 5)  # above max of 4

    output = DifferentiationPillarsOutput(differentiation_pillars=[pillar] * 2)
    assert len(output.differentiation_pillars) == 2


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


_STORY = dict(
    brand_story="Origin story.",
    hero_narrative="Punchy hero.",
    boilerplate_variants=["short", "medium", "long"],
)
_ARCHETYPE = BrandArchetype(archetype="The Creator")
_PITCHES = [
    ElevatorPitch(tier="5-second", pitch="a"),
    ElevatorPitch(tier="30-second", pitch="b"),
    ElevatorPitch(tier="2-minute", pitch="c"),
]
_PILLAR = MessagingPillar(pillar="Cohesion")
_AUDIENCE = AudienceMessageMap(audience_segment="Enterprise leaders")
_PERSONA = PersonaProfile(name="Alex")
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
