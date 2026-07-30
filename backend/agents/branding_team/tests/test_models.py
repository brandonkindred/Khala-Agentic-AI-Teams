"""Validation tests for the Phase 1 and Phase 2 structured-output wrapper models.

These agent-facing models (``BrandDiscoveryAuditOutput``, ``PurposeVisionOutput``,
``CoreValuesOutput``, ``AudienceSegmentsOutput``, ``DifferentiationPillarsOutput``,
``PositioningOutput``, plus Phase 2's ``BrandStoryOutput``,
``BrandArchetypesOutput``, ``TaglineOutput``, ``MessagingFrameworkOutput``,
``PersonaProfilesOutput``, ``WritingGuidelinesOutput``) must reject empty/omitted
content so Strands' structured-output tool retries the LLM instead of silently
accepting a blank or under-cardinality response (see ``structured_output_tool.py``:
a ``ValidationError`` becomes a tool error the model is asked to fix).
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
    BrandDiscoveryAuditOutput,
    BrandStoryOutput,
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


def test_brand_archetypes_output_enforces_stated_cardinality() -> None:
    """Prompt asks for "1-2 brand archetypes"."""
    archetype = BrandArchetype(archetype="The Creator")
    with pytest.raises(ValidationError):
        BrandArchetypesOutput(brand_archetypes=[])  # below min of 1
    with pytest.raises(ValidationError):
        BrandArchetypesOutput(brand_archetypes=[archetype] * 3)  # above max of 2

    output = BrandArchetypesOutput(brand_archetypes=[archetype])
    assert len(output.brand_archetypes) == 1


def test_tagline_output_rejects_missing_and_enforces_cardinality() -> None:
    """Prompt asks for three elevator pitch tiers."""
    pitches = [
        ElevatorPitch(tier="5-second", pitch="a"),
        ElevatorPitch(tier="30-second", pitch="b"),
        ElevatorPitch(tier="2-minute", pitch="c"),
    ]
    with pytest.raises(ValidationError):
        TaglineOutput()
    with pytest.raises(ValidationError):
        TaglineOutput(tagline="", tagline_rationale="x", elevator_pitches=pitches)
    with pytest.raises(ValidationError):
        TaglineOutput(tagline="x", tagline_rationale="y", elevator_pitches=pitches[:2])

    output = TaglineOutput(
        tagline="Ship brand", tagline_rationale="Clear", elevator_pitches=pitches
    )
    assert output.tagline == "Ship brand"


def test_messaging_framework_output_enforces_stated_cardinality() -> None:
    """Prompt asks for "3-4 messaging pillars" and at least one audience map."""
    pillar = MessagingPillar(pillar="Cohesion")
    audience_map = AudienceMessageMap(audience_segment="Enterprise leaders")
    with pytest.raises(ValidationError):
        MessagingFrameworkOutput(
            messaging_framework=[pillar, pillar],
            audience_message_maps=[audience_map],
        )
    with pytest.raises(ValidationError):
        MessagingFrameworkOutput(
            messaging_framework=[pillar] * 5,
            audience_message_maps=[audience_map],
        )
    with pytest.raises(ValidationError):
        MessagingFrameworkOutput(
            messaging_framework=[pillar] * 3,
            audience_message_maps=[],
        )

    output = MessagingFrameworkOutput(
        messaging_framework=[pillar] * 3,
        audience_message_maps=[audience_map],
    )
    assert len(output.messaging_framework) == 3


def test_persona_profiles_output_enforces_stated_cardinality() -> None:
    """Prompt asks for "2-3 persona profiles"."""
    persona = PersonaProfile(name="Alex")
    with pytest.raises(ValidationError):
        PersonaProfilesOutput(persona_profiles=[persona])  # below min of 2
    with pytest.raises(ValidationError):
        PersonaProfilesOutput(persona_profiles=[persona] * 4)  # above max of 3

    output = PersonaProfilesOutput(persona_profiles=[persona, persona])
    assert len(output.persona_profiles) == 2


def test_writing_guidelines_output_rejects_missing_and_enforces_cardinality() -> None:
    """Prompt asks for 3-4 items on each of the four lists."""
    with pytest.raises(ValidationError):
        WritingGuidelinesOutput()
    with pytest.raises(ValidationError):
        WritingGuidelinesOutput(
            voice_principles=["a", "b"],
            style_dos=["a", "b", "c"],
            style_donts=["a", "b", "c"],
            editorial_quality_bar=["a", "b", "c"],
        )

    output = WritingGuidelinesOutput(
        voice_principles=["a", "b", "c"],
        style_dos=["a", "b", "c"],
        style_donts=["a", "b", "c"],
        editorial_quality_bar=["a", "b", "c"],
    )
    assert len(output.voice_principles) == 3
