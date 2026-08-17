"""Validation tests for the Phase 1–5 structured-output models.

Agent-facing ``*Output`` models must reject empty/omitted content so Strands'
structured-output tool retries the LLM instead of silently accepting a blank
or under-cardinality response (see ``structured_output_tool.py``: a
``ValidationError`` becomes a tool error the model is asked to fix).

Collapsed nested-item models (``CoreValue``, ``AudienceSegment``,
``DifferentiationPillar``, ``BrandArchetype``, ``MessagingPillar``,
``AudienceMessageMap``, ``ElevatorPitch``, ``PersonaProfile``,
``LogoUsageRule``, ``ColorEntry``, ``TypographySpec``, ``VoiceToneEntry``,
``BrandArchitectureRule``, ``BrandInActionExample``, ``ApprovalWorkflow``,
``WikiEntry``, ``BrandHealthKPI`` and their ``*Output`` subclasses) are
covered in dual-mode: the soft base still permits blank/omitted content
(merge-target contract), the strict subclass still rejects blanks, and
``isinstance(strict, soft)`` holds because ``_derive_strict_variant``
generates a real subclass. Wrapper and compositor schemas
(``PurposeVisionOutput``, ``CoreValuesOutput``, Phase 2's cumulative chain,
Phase 4/5 compositor models) keep their cardinality and blank-rejection
tests. ``BrandDiscoveryAudit`` is a fully collapsed single model (used both
as ``discovery_auditor``'s ``structured_output`` and as
``StrategicCoreOutput.brand_discovery``'s ``default_factory``), so its
fields default to empty rather than being required.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from branding_team.models import (
    ApprovalWorkflow,
    ApprovalWorkflowOutput,
    ApprovalWorkflowsOutput,
    AudienceMessageMap,
    AudienceMessageMapOutput,
    AudienceSegment,
    AudienceSegmentOutput,
    AudienceSegmentsOutput,
    BrandArchetype,
    BrandArchetypeOutput,
    BrandArchetypesOutput,
    BrandArchitectureOutput,
    BrandArchitectureRule,
    BrandArchitectureRuleOutput,
    BrandDiscoveryAudit,
    BrandExperiencePrinciplesOutput,
    BrandHealthKPI,
    BrandHealthKPIOutput,
    BrandInActionExample,
    BrandInActionExampleOutput,
    BrandStoryOutput,
    ChannelActivationOutput,
    ChannelGuidelineOutput,
    ColorEntry,
    ColorEntryOutput,
    CoreValue,
    CoreValueOutput,
    CoreValuesOutput,
    DifferentiationPillar,
    DifferentiationPillarOutput,
    DifferentiationPillarsOutput,
    ElevatorPitch,
    ElevatorPitchOutput,
    GovernanceOutput,
    LogoUsageRule,
    LogoUsageRuleOutput,
    MessagingFrameworkOutput,
    MessagingPillar,
    MessagingPillarOutput,
    PersonaProfile,
    PersonaProfileOutput,
    PersonaProfilesOutput,
    PositioningOutput,
    PurposeVisionOutput,
    TaglineOutput,
    TypographySpec,
    TypographySpecOutput,
    VoiceToneEntry,
    VoiceToneEntryOutput,
    WikiEntry,
    WikiEntryOutput,
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


def test_brand_discovery_audit_constructs_with_no_arguments() -> None:
    """Backs StrategicCoreOutput.brand_discovery's default_factory."""
    audit = BrandDiscoveryAudit()
    assert audit.current_brand_perception == ""
    assert audit.market_position == ""
    assert audit.strengths == []
    assert audit.weaknesses == []
    assert audit.opportunities == []
    assert audit.threats == []
    assert audit.stakeholder_insights == []


def test_brand_discovery_audit_round_trips_given_fields() -> None:
    audit = BrandDiscoveryAudit(**_DISCOVERY_KWARGS)
    assert audit.current_brand_perception == "Seen as reliable but generic."
    assert audit.market_position == "Mid-market challenger."
    assert audit.strengths == ["Delivery speed"]
    assert audit.weaknesses == ["Low brand recall"]
    assert audit.opportunities == ["Category consolidating"]
    assert audit.threats == ["Bigger competitors out-spending"]
    assert audit.stakeholder_insights == ["Sales wants sharper differentiation"]


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


def test_core_value_permits_blank_and_omitted_content() -> None:
    """``CoreValue`` is the soft merge-target twin: only ``value`` is
    required; ``behavioral_definition``/``observable_behaviors`` accept
    blank/empty content, matching ``StrategicCoreOutput.core_values``'s
    partial-fragment merge contract."""
    minimal = CoreValue(value="clarity")
    assert minimal.behavioral_definition == ""
    assert minimal.observable_behaviors == []

    explicit_blank = CoreValue(value="clarity", behavioral_definition="", observable_behaviors=[])
    assert explicit_blank.behavioral_definition == ""
    assert explicit_blank.observable_behaviors == []

    blank_item = CoreValue(value="clarity", observable_behaviors=[""])
    assert blank_item.observable_behaviors == [""]


def test_core_value_output_is_usable_as_a_core_value() -> None:
    """The derived strict twin stays a normal, directly constructible
    ``pydantic.BaseModel`` subclass wherever ``CoreValue`` is."""
    output = CoreValueOutput(
        value="clarity",
        behavioral_definition="We demonstrate clarity in every decision.",
        observable_behaviors=["Plain-language docs"],
    )
    assert isinstance(output, CoreValue)


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


def test_audience_segment_permits_blank_and_omitted_content() -> None:
    """``AudienceSegment`` is the soft merge-target twin: only ``name`` is
    required; ``description``/``pain_points``/``goals``/``decision_drivers``
    accept blank/omitted content, matching
    ``StrategicCoreOutput.target_audience_segments``'s partial-fragment merge
    contract."""
    minimal = AudienceSegment(name="Enterprise leaders")
    assert minimal.description == ""
    assert minimal.pain_points == []
    assert minimal.goals == []
    assert minimal.decision_drivers == []

    explicit_blank = AudienceSegment(
        name="Enterprise leaders",
        description="",
        pain_points=[],
        goals=[],
        decision_drivers=[],
    )
    assert explicit_blank.description == ""

    blank_item = AudienceSegment(name="Enterprise leaders", pain_points=[""])
    assert blank_item.pain_points == [""]


def test_audience_segment_output_is_usable_as_an_audience_segment() -> None:
    """The derived strict twin stays a normal, directly constructible
    ``pydantic.BaseModel`` subclass wherever ``AudienceSegment`` is."""
    output = AudienceSegmentOutput(
        name="Enterprise leaders",
        description="VP/Director-level buyers at mid-market SaaS companies.",
        pain_points=["Inconsistent brand touchpoints"],
        goals=["Ship cohesive experiences"],
        decision_drivers=["Proven execution speed"],
    )
    assert isinstance(output, AudienceSegment)


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


def test_differentiation_pillar_permits_blank_and_omitted_content() -> None:
    """``DifferentiationPillar`` is the soft merge-target twin: only
    ``pillar`` is required; ``proof_points``/``competitive_context`` accept
    blank/omitted content, matching
    ``StrategicCoreOutput.differentiation_pillars``'s partial-fragment merge
    contract."""
    minimal = DifferentiationPillar(pillar="Execution speed")
    assert minimal.proof_points == []
    assert minimal.competitive_context == ""

    explicit_blank = DifferentiationPillar(
        pillar="Execution speed", proof_points=[], competitive_context=""
    )
    assert explicit_blank.competitive_context == ""

    blank_item = DifferentiationPillar(pillar="Execution speed", proof_points=[""])
    assert blank_item.proof_points == [""]


def test_differentiation_pillar_output_is_usable_as_a_differentiation_pillar() -> None:
    """The derived strict twin stays a normal, directly constructible
    ``pydantic.BaseModel`` subclass wherever ``DifferentiationPillar`` is."""
    output = DifferentiationPillarOutput(
        pillar="Execution speed",
        proof_points=["Ship weekly release cadence"],
        competitive_context="Competitors ship quarterly.",
    )
    assert isinstance(output, DifferentiationPillar)


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


def test_brand_archetype_permits_blank_and_omitted_content() -> None:
    """``BrandArchetype`` is the soft merge-target twin: only ``archetype``
    is required; ``rationale``/``personality_traits`` accept blank/omitted
    content, matching ``NarrativeMessagingOutput.brand_archetypes``'s
    partial-fragment merge contract."""
    minimal = BrandArchetype(archetype="The Creator")
    assert minimal.rationale == ""
    assert minimal.personality_traits == []

    blank_item = BrandArchetype(archetype="The Creator", personality_traits=[""])
    assert blank_item.personality_traits == [""]


def test_brand_archetype_output_is_usable_as_a_brand_archetype() -> None:
    """The derived strict twin stays a normal, directly constructible
    ``pydantic.BaseModel`` subclass wherever ``BrandArchetype`` is."""
    assert isinstance(_ARCHETYPE, BrandArchetype)


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


def test_elevator_pitch_permits_blank_and_omitted_content() -> None:
    """``ElevatorPitch`` is the soft merge-target twin: both fields default
    to blank, matching ``NarrativeMessagingOutput.elevator_pitches``'s
    partial-fragment merge contract."""
    minimal = ElevatorPitch()
    assert minimal.tier == ""
    assert minimal.pitch == ""

    explicit_blank = ElevatorPitch(tier="", pitch="")
    assert explicit_blank.tier == ""
    assert explicit_blank.pitch == ""


def test_elevator_pitch_output_is_usable_as_an_elevator_pitch() -> None:
    """The derived strict twin stays a normal, directly constructible
    ``pydantic.BaseModel`` subclass wherever ``ElevatorPitch`` is."""
    assert isinstance(_PITCHES[0], ElevatorPitch)


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


def test_messaging_pillar_permits_blank_and_omitted_content() -> None:
    """``MessagingPillar`` is the soft merge-target twin: only ``pillar`` is
    required; ``key_message``/``proof_points`` accept blank/omitted content,
    matching ``NarrativeMessagingOutput.messaging_framework``'s
    partial-fragment merge contract."""
    minimal = MessagingPillar(pillar="Cohesion")
    assert minimal.key_message == ""
    assert minimal.proof_points == []

    blank_item = MessagingPillar(pillar="Cohesion", proof_points=[""])
    assert blank_item.proof_points == [""]


def test_messaging_pillar_output_is_usable_as_a_messaging_pillar() -> None:
    """The derived strict twin stays a normal, directly constructible
    ``pydantic.BaseModel`` subclass wherever ``MessagingPillar`` is."""
    assert isinstance(_PILLAR, MessagingPillar)


def test_audience_message_map_permits_blank_and_omitted_content() -> None:
    """``AudienceMessageMap`` is the soft merge-target twin: only
    ``audience_segment`` is required; ``primary_message``/
    ``supporting_messages``/``tone_adjustments`` accept blank/omitted
    content, matching ``NarrativeMessagingOutput.audience_message_maps``'s
    partial-fragment merge contract."""
    minimal = AudienceMessageMap(audience_segment="Enterprise leaders")
    assert minimal.primary_message == ""
    assert minimal.supporting_messages == []
    assert minimal.tone_adjustments == ""

    blank_item = AudienceMessageMap(audience_segment="Enterprise leaders", supporting_messages=[""])
    assert blank_item.supporting_messages == [""]


def test_audience_message_map_output_is_usable_as_an_audience_message_map() -> None:
    """The derived strict twin stays a normal, directly constructible
    ``pydantic.BaseModel`` subclass wherever ``AudienceMessageMap`` is."""
    assert isinstance(_AUDIENCE, AudienceMessageMap)


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


def test_persona_profile_permits_blank_and_omitted_content() -> None:
    """``PersonaProfile`` is the soft merge-target twin: only ``name`` is
    required; every other field accepts blank/omitted content, matching
    ``NarrativeMessagingOutput.persona_profiles``'s partial-fragment merge
    contract."""
    minimal = PersonaProfile(name="Alex")
    assert minimal.role == ""
    assert minimal.demographics == ""
    assert minimal.psychographics == ""
    assert minimal.goals == []
    assert minimal.frustrations == []
    assert minimal.media_habits == []
    assert minimal.jobs_to_be_done == []

    blank_item = PersonaProfile(name="Alex", goals=[""])
    assert blank_item.goals == [""]


def test_persona_profile_output_is_usable_as_a_persona_profile() -> None:
    """The derived strict twin stays a normal, directly constructible
    ``pydantic.BaseModel`` subclass wherever ``PersonaProfile`` is."""
    assert isinstance(_PERSONA, PersonaProfile)


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


def test_logo_usage_rule_permits_blank_and_omitted_content() -> None:
    """``LogoUsageRule`` is the soft merge-target twin: all fields default
    to empty, matching ``VisualIdentityOutput.logo_suite``'s partial-fragment
    merge contract."""
    minimal = LogoUsageRule()
    assert minimal.variant == ""
    assert minimal.usage_context == ""
    assert minimal.minimum_size == ""
    assert minimal.clear_space == ""

    explicit_blank = LogoUsageRule(variant="", usage_context="", minimum_size="", clear_space="")
    assert explicit_blank.variant == ""


def test_logo_usage_rule_output_rejects_blank_content() -> None:
    """A blank variant, usage context, minimum size, or clear space must fail."""
    valid_kwargs = dict(
        variant="primary",
        usage_context="Full-color lockup on light backgrounds",
        minimum_size="24px",
        clear_space="0.5x cap-height",
    )

    with pytest.raises(ValidationError):
        LogoUsageRuleOutput(**{**valid_kwargs, "variant": ""})
    with pytest.raises(ValidationError):
        LogoUsageRuleOutput(**{**valid_kwargs, "usage_context": ""})
    with pytest.raises(ValidationError):
        LogoUsageRuleOutput(**{**valid_kwargs, "minimum_size": ""})
    with pytest.raises(ValidationError):
        LogoUsageRuleOutput(**{**valid_kwargs, "clear_space": ""})

    output = LogoUsageRuleOutput(**valid_kwargs)
    assert output.variant == "primary"


def test_logo_usage_rule_output_is_usable_as_a_logo_usage_rule() -> None:
    """The derived strict twin stays a normal, directly constructible
    ``pydantic.BaseModel`` subclass wherever ``LogoUsageRule`` is."""
    output = LogoUsageRuleOutput(
        variant="primary",
        usage_context="Full-color lockup on light backgrounds",
        minimum_size="24px",
        clear_space="0.5x cap-height",
    )
    assert isinstance(output, LogoUsageRule)


def test_color_entry_permits_blank_and_omitted_content() -> None:
    """``ColorEntry`` is the soft merge-target twin: only ``name`` is
    required; ``hex_value``/``usage``/``psychological_rationale`` accept
    blank/omitted content, matching ``VisualIdentityOutput.color_palette``'s
    partial-fragment merge contract."""
    minimal = ColorEntry(name="Midnight")
    assert minimal.hex_value == ""
    assert minimal.usage == ""
    assert minimal.psychological_rationale == ""

    explicit_blank = ColorEntry(name="Midnight", hex_value="", usage="", psychological_rationale="")
    assert explicit_blank.hex_value == ""


def test_color_entry_output_rejects_blank_content() -> None:
    """A blank name, hex value, usage, or rationale must fail validation."""
    valid_kwargs = dict(
        name="Midnight",
        hex_value="#1a1a2e",
        usage="Primary background",
        psychological_rationale="Conveys depth and authority",
    )

    with pytest.raises(ValidationError):
        ColorEntryOutput(**{**valid_kwargs, "name": ""})
    with pytest.raises(ValidationError):
        ColorEntryOutput(**{**valid_kwargs, "hex_value": ""})
    with pytest.raises(ValidationError):
        ColorEntryOutput(**{**valid_kwargs, "usage": ""})
    with pytest.raises(ValidationError):
        ColorEntryOutput(**{**valid_kwargs, "psychological_rationale": ""})

    output = ColorEntryOutput(**valid_kwargs)
    assert output.name == "Midnight"


def test_color_entry_output_is_usable_as_a_color_entry() -> None:
    """The derived strict twin stays a normal, directly constructible
    ``pydantic.BaseModel`` subclass wherever ``ColorEntry`` is."""
    output = ColorEntryOutput(
        name="Midnight",
        hex_value="#1a1a2e",
        usage="Primary background",
        psychological_rationale="Conveys depth and authority",
    )
    assert isinstance(output, ColorEntry)


def test_typography_spec_permits_blank_and_omitted_content() -> None:
    """``TypographySpec`` is the soft merge-target twin: all fields default
    to empty, matching ``VisualIdentityOutput.typography_system``'s
    partial-fragment merge contract."""
    minimal = TypographySpec()
    assert minimal.role == ""
    assert minimal.font_family == ""
    assert minimal.weight_range == ""
    assert minimal.usage_notes == ""

    explicit_blank = TypographySpec(role="", font_family="", weight_range="", usage_notes="")
    assert explicit_blank.role == ""


def test_typography_spec_output_rejects_blank_content() -> None:
    """A blank role, font family, weight range, or usage notes must fail."""
    valid_kwargs = dict(
        role="display",
        font_family="Inter",
        weight_range="600-800",
        usage_notes="Headlines and hero type only",
    )

    with pytest.raises(ValidationError):
        TypographySpecOutput(**{**valid_kwargs, "role": ""})
    with pytest.raises(ValidationError):
        TypographySpecOutput(**{**valid_kwargs, "font_family": ""})
    with pytest.raises(ValidationError):
        TypographySpecOutput(**{**valid_kwargs, "weight_range": ""})
    with pytest.raises(ValidationError):
        TypographySpecOutput(**{**valid_kwargs, "usage_notes": ""})

    output = TypographySpecOutput(**valid_kwargs)
    assert output.role == "display"


def test_typography_spec_output_is_usable_as_a_typography_spec() -> None:
    """The derived strict twin stays a normal, directly constructible
    ``pydantic.BaseModel`` subclass wherever ``TypographySpec`` is."""
    output = TypographySpecOutput(
        role="display",
        font_family="Inter",
        weight_range="600-800",
        usage_notes="Headlines and hero type only",
    )
    assert isinstance(output, TypographySpec)


def test_voice_tone_entry_permits_blank_and_omitted_content() -> None:
    """``VoiceToneEntry`` is the soft merge-target twin: all fields default
    empty, matching ``VisualIdentityOutput.voice_tone_spectrum``'s
    partial-fragment merge contract."""
    minimal = VoiceToneEntry()
    assert minimal.context == ""
    assert minimal.tone == ""
    assert minimal.examples == []

    explicit_blank = VoiceToneEntry(context="", tone="", examples=[])
    assert explicit_blank.examples == []

    blank_item = VoiceToneEntry(examples=[""])
    assert blank_item.examples == [""]


def test_voice_tone_entry_output_rejects_blank_content() -> None:
    """A blank context, tone, or empty examples list must fail; a list of
    blank strings is still valid because ``examples`` is ``List[str]`` with
    container ``min_length=1``, not ``List[NonEmptyStr]``."""
    valid_kwargs = dict(
        context="marketing",
        tone="confident and warm",
        examples=["Let's ship the brand, not the buzzwords."],
    )

    with pytest.raises(ValidationError):
        VoiceToneEntryOutput(**{**valid_kwargs, "context": ""})
    with pytest.raises(ValidationError):
        VoiceToneEntryOutput(**{**valid_kwargs, "tone": ""})
    with pytest.raises(ValidationError):
        VoiceToneEntryOutput(**{**valid_kwargs, "examples": []})

    output = VoiceToneEntryOutput(**valid_kwargs)
    assert output.context == "marketing"

    blank_item = VoiceToneEntryOutput(context="marketing", tone="confident and warm", examples=[""])
    assert blank_item.examples == [""]


def test_voice_tone_entry_output_is_usable_as_a_voice_tone_entry() -> None:
    """The derived strict twin stays a normal, directly constructible
    ``pydantic.BaseModel`` subclass wherever ``VoiceToneEntry`` is."""
    output = VoiceToneEntryOutput(
        context="marketing",
        tone="confident and warm",
        examples=["Let's ship the brand, not the buzzwords."],
    )
    assert isinstance(output, VoiceToneEntry)


def test_brand_architecture_rule_permits_blank_and_omitted_content() -> None:
    """``BrandArchitectureRule`` is the soft merge-target twin: all fields
    default empty, matching ``ChannelActivationOutput.brand_architecture``'s
    partial-fragment merge contract."""
    minimal = BrandArchitectureRule()
    assert minimal.entity == ""
    assert minimal.relationship == ""
    assert minimal.naming_convention == ""
    assert minimal.visual_treatment == ""

    explicit_blank = BrandArchitectureRule(
        entity="", relationship="", naming_convention="", visual_treatment=""
    )
    assert explicit_blank.entity == ""


def test_brand_architecture_rule_output_rejects_blank_content() -> None:
    """A blank entity, relationship, naming convention, or visual treatment must fail."""
    valid_kwargs = dict(
        entity="Parent brand",
        relationship="Umbrella over sub-brands",
        naming_convention="[Parent] [Product]",
        visual_treatment="Shared wordmark, distinct accent color",
    )

    with pytest.raises(ValidationError):
        BrandArchitectureRuleOutput(**{**valid_kwargs, "entity": ""})
    with pytest.raises(ValidationError):
        BrandArchitectureRuleOutput(**{**valid_kwargs, "relationship": ""})
    with pytest.raises(ValidationError):
        BrandArchitectureRuleOutput(**{**valid_kwargs, "naming_convention": ""})
    with pytest.raises(ValidationError):
        BrandArchitectureRuleOutput(**{**valid_kwargs, "visual_treatment": ""})

    output = BrandArchitectureRuleOutput(**valid_kwargs)
    assert output.entity == "Parent brand"


def test_brand_architecture_rule_output_is_usable_as_a_brand_architecture_rule() -> None:
    """The derived strict twin stays a normal, directly constructible
    ``pydantic.BaseModel`` subclass wherever ``BrandArchitectureRule`` is."""
    output = BrandArchitectureRuleOutput(
        entity="Parent brand",
        relationship="Umbrella over sub-brands",
        naming_convention="[Parent] [Product]",
        visual_treatment="Shared wordmark, distinct accent color",
    )
    assert isinstance(output, BrandArchitectureRule)


def test_brand_in_action_example_permits_blank_and_omitted_content() -> None:
    """``BrandInActionExample`` is the soft merge-target twin: all fields
    default empty, matching ``ChannelActivationOutput.brand_in_action``'s
    partial-fragment merge contract."""
    minimal = BrandInActionExample()
    assert minimal.context == ""
    assert minimal.correct_example == ""
    assert minimal.incorrect_example == ""
    assert minimal.rationale == ""

    explicit_blank = BrandInActionExample(
        context="", correct_example="", incorrect_example="", rationale=""
    )
    assert explicit_blank.context == ""


def test_brand_in_action_example_output_rejects_blank_content() -> None:
    """A blank context, correct example, incorrect example, or rationale must fail."""
    valid_kwargs = dict(
        context="Homepage hero",
        correct_example="Lead with the brand promise, then proof.",
        incorrect_example="Lead with a generic stock photo and slogan.",
        rationale="First impression must match positioning.",
    )

    with pytest.raises(ValidationError):
        BrandInActionExampleOutput(**{**valid_kwargs, "context": ""})
    with pytest.raises(ValidationError):
        BrandInActionExampleOutput(**{**valid_kwargs, "correct_example": ""})
    with pytest.raises(ValidationError):
        BrandInActionExampleOutput(**{**valid_kwargs, "incorrect_example": ""})
    with pytest.raises(ValidationError):
        BrandInActionExampleOutput(**{**valid_kwargs, "rationale": ""})

    output = BrandInActionExampleOutput(**valid_kwargs)
    assert output.context == "Homepage hero"


def test_brand_in_action_example_output_is_usable_as_a_brand_in_action_example() -> None:
    """The derived strict twin stays a normal, directly constructible
    ``pydantic.BaseModel`` subclass wherever ``BrandInActionExample`` is."""
    output = BrandInActionExampleOutput(
        context="Homepage hero",
        correct_example="Lead with the brand promise, then proof.",
        incorrect_example="Lead with a generic stock photo and slogan.",
        rationale="First impression must match positioning.",
    )
    assert isinstance(output, BrandInActionExample)


def test_approval_workflow_permits_blank_and_omitted_content() -> None:
    """``ApprovalWorkflow`` is the soft merge-target twin: all fields default
    empty, matching ``GovernanceOutput.approval_workflows``'s partial-fragment
    merge contract."""
    minimal = ApprovalWorkflow()
    assert minimal.asset_type == ""
    assert minimal.approvers == []
    assert minimal.sla == ""
    assert minimal.escalation_path == ""

    explicit_blank = ApprovalWorkflow(asset_type="", approvers=[], sla="", escalation_path="")
    assert explicit_blank.approvers == []

    blank_item = ApprovalWorkflow(approvers=[""])
    assert blank_item.approvers == [""]


def test_approval_workflow_output_rejects_blank_content() -> None:
    """A blank asset type, SLA, escalation path, empty approvers list, or
    blank approver item must fail."""
    valid_kwargs = dict(
        asset_type="Campaign landing page",
        approvers=["Brand Director"],
        sla="2 business days",
        escalation_path="Brand Director -> CMO",
    )

    with pytest.raises(ValidationError):
        ApprovalWorkflowOutput(**{**valid_kwargs, "asset_type": ""})
    with pytest.raises(ValidationError):
        ApprovalWorkflowOutput(**{**valid_kwargs, "approvers": []})
    with pytest.raises(ValidationError):
        ApprovalWorkflowOutput(**{**valid_kwargs, "approvers": [""]})
    with pytest.raises(ValidationError):
        ApprovalWorkflowOutput(**{**valid_kwargs, "sla": ""})
    with pytest.raises(ValidationError):
        ApprovalWorkflowOutput(**{**valid_kwargs, "escalation_path": ""})

    output = ApprovalWorkflowOutput(**valid_kwargs)
    assert output.asset_type == "Campaign landing page"


def test_approval_workflow_output_is_usable_as_an_approval_workflow() -> None:
    """The derived strict twin stays a normal, directly constructible
    ``pydantic.BaseModel`` subclass wherever ``ApprovalWorkflow`` is."""
    output = ApprovalWorkflowOutput(
        asset_type="Campaign landing page",
        approvers=["Brand Director"],
        sla="2 business days",
        escalation_path="Brand Director -> CMO",
    )
    assert isinstance(output, ApprovalWorkflow)


def test_wiki_entry_permits_blank_and_omitted_content() -> None:
    """``WikiEntry`` is the soft merge-target twin: ``title``/``summary`` are
    required but unconstrained, ``owners`` defaults empty, and
    ``update_cadence`` defaults to ``monthly``, matching
    ``GovernanceOutput.wiki_backlog``'s partial-fragment merge contract."""
    minimal = WikiEntry(title="Brand North Star", summary="Source of truth for positioning.")
    assert minimal.owners == []
    assert minimal.update_cadence == "monthly"

    explicit_blank = WikiEntry(title="", summary="", owners=[], update_cadence="")
    assert explicit_blank.title == ""
    assert explicit_blank.summary == ""
    assert explicit_blank.update_cadence == ""

    blank_item = WikiEntry(title="Brand North Star", summary="Source of truth.", owners=[""])
    assert blank_item.owners == [""]


def test_wiki_entry_output_rejects_blank_content() -> None:
    """A blank title, summary, cadence, empty owners list, or blank owner must fail."""
    valid_kwargs = dict(
        title="Brand North Star",
        summary="Source of truth for positioning.",
        owners=["Brand Strategy"],
        update_cadence="quarterly",
    )

    with pytest.raises(ValidationError):
        WikiEntryOutput(**{**valid_kwargs, "title": ""})
    with pytest.raises(ValidationError):
        WikiEntryOutput(**{**valid_kwargs, "summary": ""})
    with pytest.raises(ValidationError):
        WikiEntryOutput(**{**valid_kwargs, "owners": []})
    with pytest.raises(ValidationError):
        WikiEntryOutput(**{**valid_kwargs, "owners": [""]})
    with pytest.raises(ValidationError):
        WikiEntryOutput(**{**valid_kwargs, "update_cadence": ""})

    output = WikiEntryOutput(**valid_kwargs)
    assert output.title == "Brand North Star"


def test_wiki_entry_output_is_usable_as_a_wiki_entry() -> None:
    """The derived strict twin stays a normal, directly constructible
    ``pydantic.BaseModel`` subclass wherever ``WikiEntry`` is."""
    output = WikiEntryOutput(
        title="Brand North Star",
        summary="Source of truth for positioning.",
        owners=["Brand Strategy"],
        update_cadence="quarterly",
    )
    assert isinstance(output, WikiEntry)


def test_brand_health_kpi_permits_blank_and_omitted_content() -> None:
    """``BrandHealthKPI`` is the soft merge-target twin: all fields default
    empty, matching ``GovernanceOutput.brand_health_kpis``'s partial-fragment
    merge contract."""
    minimal = BrandHealthKPI()
    assert minimal.metric == ""
    assert minimal.measurement_method == ""
    assert minimal.target == ""
    assert minimal.review_frequency == ""

    explicit_blank = BrandHealthKPI(
        metric="", measurement_method="", target="", review_frequency=""
    )
    assert explicit_blank.metric == ""


def test_brand_health_kpi_output_rejects_blank_content() -> None:
    """A blank metric, measurement method, target, or review frequency must fail."""
    valid_kwargs = dict(
        metric="NPS",
        measurement_method="Quarterly survey",
        target=">50",
        review_frequency="quarterly",
    )

    with pytest.raises(ValidationError):
        BrandHealthKPIOutput(**{**valid_kwargs, "metric": ""})
    with pytest.raises(ValidationError):
        BrandHealthKPIOutput(**{**valid_kwargs, "measurement_method": ""})
    with pytest.raises(ValidationError):
        BrandHealthKPIOutput(**{**valid_kwargs, "target": ""})
    with pytest.raises(ValidationError):
        BrandHealthKPIOutput(**{**valid_kwargs, "review_frequency": ""})

    output = BrandHealthKPIOutput(**valid_kwargs)
    assert output.metric == "NPS"


def test_brand_health_kpi_output_is_usable_as_a_brand_health_kpi() -> None:
    """The derived strict twin stays a normal, directly constructible
    ``pydantic.BaseModel`` subclass wherever ``BrandHealthKPI`` is."""
    output = BrandHealthKPIOutput(
        metric="NPS",
        measurement_method="Quarterly survey",
        target=">50",
        review_frequency="quarterly",
    )
    assert isinstance(output, BrandHealthKPI)


def test_channel_activation_accepts_strict_architecture_fragment_dump() -> None:
    """``_merge_structured_output`` validates a specialist dump against the
    soft phase model; a ``BrandArchitectureOutput`` dump must still merge
    into ``ChannelActivationOutput`` after the nested twin collapse."""
    merged = ChannelActivationOutput.model_validate(
        BrandArchitectureOutput(**_ARCHITECTURE_KWARGS).model_dump()
    )
    assert len(merged.brand_architecture) == 1
    assert merged.brand_architecture[0].entity == "Parent brand"
    assert isinstance(merged.brand_architecture[0], BrandArchitectureRule)


def test_governance_accepts_strict_workflow_fragment_dump() -> None:
    """``_merge_structured_output`` validates a specialist dump against the
    soft phase model; an ``ApprovalWorkflowsOutput`` dump must still merge
    into ``GovernanceOutput`` after the nested twin collapse."""
    workflow = ApprovalWorkflowOutput(
        asset_type="Campaign landing page",
        approvers=["Brand Director"],
        sla="2 business days",
        escalation_path="Brand Director -> CMO",
    )
    fragment = ApprovalWorkflowsOutput(
        approval_workflows=[workflow, workflow, workflow],
        agency_briefing_protocols=[
            "Always include brand book",
            "Share latest palette",
            "Cite wiki owners",
        ],
    )
    merged = GovernanceOutput.model_validate(fragment.model_dump())
    assert len(merged.approval_workflows) == 3
    assert merged.approval_workflows[0].asset_type == "Campaign landing page"
    assert isinstance(merged.approval_workflows[0], ApprovalWorkflow)
