"""Contract tests binding the dummy LLM payloads to the branding agent schemas.

Every branding agent built with ``structured_output=`` hands Strands a Pydantic
model that the provider response *must* validate against. Under
``LLM_PROVIDER=dummy`` that response comes from ``DummyLLMClient.complete_json``,
which routes on substrings of the agent's system prompt. Nothing else in the
suite drives those two halves against each other, so three independent edits can
silently break the no-LLM harness:

1. tightening a model in ``branding_team.models`` (adding a field, a ``Literal``,
   an enum, or a length bound) past what the dummy stub supplies;
2. editing a dummy payload in ``llm_service.clients.dummy`` so it no longer
   satisfies its model;
3. rewording an agent's system prompt in ``branding_team.agents`` so it stops
   matching its dummy routing branch and falls through to the generic
   ``{"status": ..., "output": ...}`` fallback.

All three surface here as a failure naming the offending agent. Case (3) is the
one that needed follow-up commits when Phases 3 and 4 migrated to
``structured_output=``, and ``test_generic_prompt_payload_is_rejected_by_every_schema``
is what keeps the primary assertion honest about catching it.

The seven Phase 5 governance factories are listed in
``_FACTORIES_WITHOUT_STRUCTURED_OUTPUT`` rather than covered here: they still use
prose ``output_mode`` and have no schema to bind.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

import pytest

from branding_team import agents as branding_agents
from branding_team.graphs.phase3_visual import _PHASE3_CONCEPTUALIST_VARIANTS
from branding_team.graphs.shared import serialize_mission
from branding_team.models import (
    AudienceSegmentsOutput,
    BrandArchetypesOutput,
    BrandArchitectureOutput,
    BrandDiscoveryAuditOutput,
    BrandExperiencePrinciplesOutput,
    BrandInActionOutput,
    BrandStoryOutput,
    ChannelGuidelineOutput,
    ColorPaletteSystemOutput,
    CoreValuesOutput,
    CreativeRefinementDecisionOutput,
    DesignSystemDefinitionOutput,
    DifferentiationPillarsOutput,
    IconographyOutput,
    LogoSuiteOutput,
    MessagingFrameworkOutput,
    MoodBoardCandidatesOutput,
    MoodBoardConceptOutput,
    PersonaProfilesOutput,
    PhotographyVideoOutput,
    PositioningOutput,
    PurposeVisionOutput,
    TaglineOutput,
    TypographySystemOutput,
    VoiceToneOutput,
    WritingGuidelinesOutput,
)
from branding_team.tests.conftest import make_mission
from llm_service.clients.dummy import DummyLLMClient

# A system prompt with none of the branding routing anchors, used to prove the
# dummy's agent-specific branches — not a permissive schema — are what make the
# primary assertion pass.
_UNROUTED_SYSTEM_PROMPT = "You are a helpful assistant."

# ---------------------------------------------------------------------------
# The contract table: one entry per branding agent instance that Strands will
# force a structured output on, paired with the model it must validate against.
# ---------------------------------------------------------------------------

_CASES: tuple[tuple[str, Callable[[], Any], type], ...] = (
    # Phase 1 — Strategic Core
    ("discovery_auditor", branding_agents.make_discovery_auditor, BrandDiscoveryAuditOutput),
    ("purpose_vision_writer", branding_agents.make_purpose_vision_writer, PurposeVisionOutput),
    ("values_articulator", branding_agents.make_values_articulator, CoreValuesOutput),
    ("audience_segmenter", branding_agents.make_audience_segmenter, AudienceSegmentsOutput),
    (
        "differentiation_mapper",
        branding_agents.make_differentiation_mapper,
        DifferentiationPillarsOutput,
    ),
    ("positioning_synthesizer", branding_agents.make_positioning_synthesizer, PositioningOutput),
    # Phase 2 — Narrative & Messaging
    ("storyteller", branding_agents.make_storyteller, BrandStoryOutput),
    ("archetype_analyst", branding_agents.make_archetype_analyst, BrandArchetypesOutput),
    ("tagline_writer", branding_agents.make_tagline_writer, TaglineOutput),
    ("message_mapper", branding_agents.make_message_mapper, MessagingFrameworkOutput),
    ("persona_builder", branding_agents.make_persona_builder, PersonaProfilesOutput),
    (
        "voice_principles_drafter",
        branding_agents.make_voice_principles_drafter,
        WritingGuidelinesOutput,
    ),
    # Phase 3 — Visual Identity
    ("creative_director", branding_agents.make_creative_director, MoodBoardCandidatesOutput),
    *(
        (
            f"moodboard_conceptualist_{variant.lower()}",
            # Bind the variant in a default arg so each lambda closes over its
            # own value rather than the loop's last one.
            (lambda v=variant: branding_agents.make_moodboard_conceptualist(v)),
            MoodBoardConceptOutput,
        )
        for variant in _PHASE3_CONCEPTUALIST_VARIANTS
    ),
    ("converge_decider", branding_agents.make_converge_decider, CreativeRefinementDecisionOutput),
    ("logo_specifier", branding_agents.make_logo_specifier, LogoSuiteOutput),
    ("color_system_builder", branding_agents.make_color_system_builder, ColorPaletteSystemOutput),
    ("typography_builder", branding_agents.make_typography_builder, TypographySystemOutput),
    ("iconography_director", branding_agents.make_iconography_director, IconographyOutput),
    (
        "photography_video_director",
        branding_agents.make_photography_video_director,
        PhotographyVideoOutput,
    ),
    # Phase 4 — Experience & Channel Activation
    ("voice_tone_builder", branding_agents.make_voice_tone_builder, VoiceToneOutput),
    (
        "design_system_codifier",
        branding_agents.make_design_system_codifier,
        DesignSystemDefinitionOutput,
    ),
    (
        "brand_experience_principler",
        branding_agents.make_brand_experience_principler,
        BrandExperiencePrinciplesOutput,
    ),
    ("website_guide", branding_agents.make_website_guide, ChannelGuidelineOutput),
    ("social_guide", branding_agents.make_social_guide, ChannelGuidelineOutput),
    ("email_guide", branding_agents.make_email_guide, ChannelGuidelineOutput),
    ("events_guide", branding_agents.make_events_guide, ChannelGuidelineOutput),
    ("partnerships_guide", branding_agents.make_partnerships_guide, ChannelGuidelineOutput),
    ("internal_guide", branding_agents.make_internal_guide, ChannelGuidelineOutput),
    (
        "brand_architecture_builder",
        branding_agents.make_brand_architecture_builder,
        BrandArchitectureOutput,
    ),
    (
        "brand_in_action_illustrator",
        branding_agents.make_brand_in_action_illustrator,
        BrandInActionOutput,
    ),
)

_CASE_IDS: tuple[str, ...] = tuple(case_id for case_id, _factory, _model in _CASES)

# Model classes DummyLLMClient.complete_json routes by structured_output_model
# class identity (see _branding_phase2_structured_output_stub in
# llm_service.clients.dummy) rather than by scanning system-prompt text.
# For these six, an unrouted/misleading prompt no longer prevents a valid
# payload — that is the point of issue #4252's fix, and is asserted
# separately in test_model_routed_payload_validates_regardless_of_prompt_text
# below instead of test_generic_prompt_payload_is_rejected_by_every_schema.
_MODEL_ROUTED_CLASS_NAMES: frozenset[str] = frozenset(
    {
        "BrandStoryOutput",
        "BrandArchetypesOutput",
        "TaglineOutput",
        "MessagingFrameworkOutput",
        "PersonaProfilesOutput",
        "WritingGuidelinesOutput",
    }
)
_TEXT_ROUTED_CASES: tuple[tuple[str, Callable[[], Any], type], ...] = tuple(
    case for case in _CASES if case[2].__name__ not in _MODEL_ROUTED_CLASS_NAMES
)
_TEXT_ROUTED_CASE_IDS: tuple[str, ...] = tuple(
    case_id for case_id, _factory, _model in _TEXT_ROUTED_CASES
)
_MODEL_ROUTED_CASES: tuple[tuple[str, Callable[[], Any], type], ...] = tuple(
    case for case in _CASES if case[2].__name__ in _MODEL_ROUTED_CLASS_NAMES
)
_MODEL_ROUTED_CASE_IDS: tuple[str, ...] = tuple(
    case_id for case_id, _factory, _model in _MODEL_ROUTED_CASES
)

# Factory names covered by ``_CASES``. Parameterized factories appear once here
# and expand to several table rows, so this is not derivable from ``_CASE_IDS``.
_FACTORIES_WITH_STRUCTURED_OUTPUT: frozenset[str] = frozenset(
    {
        "make_discovery_auditor",
        "make_purpose_vision_writer",
        "make_values_articulator",
        "make_audience_segmenter",
        "make_differentiation_mapper",
        "make_positioning_synthesizer",
        "make_storyteller",
        "make_archetype_analyst",
        "make_tagline_writer",
        "make_message_mapper",
        "make_persona_builder",
        "make_voice_principles_drafter",
        "make_creative_director",
        "make_moodboard_conceptualist",
        "make_converge_decider",
        "make_logo_specifier",
        "make_color_system_builder",
        "make_typography_builder",
        "make_iconography_director",
        "make_photography_video_director",
        "make_voice_tone_builder",
        "make_design_system_codifier",
        "make_brand_experience_principler",
        "make_website_guide",
        "make_social_guide",
        "make_email_guide",
        "make_events_guide",
        "make_partnerships_guide",
        "make_internal_guide",
        "make_brand_architecture_builder",
        "make_brand_in_action_illustrator",
    }
)

# Phase 5 — Governance. Still prose ``output_mode``, so there is no schema to
# bind. Move a name out of this set and into the table above when its agent
# migrates to ``structured_output=``.
_FACTORIES_WITHOUT_STRUCTURED_OUTPUT: frozenset[str] = frozenset(
    {
        "make_ownership_definer",
        "make_approval_workflow_designer",
        "make_asset_wiki_planner",
        "make_training_planner",
        "make_kpi_designer",
        "make_evolution_framer",
        "make_brand_rules_codifier",
    }
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _drive_structured_output(output_model: type, system_prompt: str) -> Any:
    """Run one ``DummyLLMClient.structured_output`` turn and return its output.

    The user turn is a serialized ``BrandingMission``, matching what every
    branding agent actually receives — the dummy aggregates user text for
    routing, so a realistic turn keeps this faithful to production.

    Preconditions:
        ``output_model`` is a Pydantic model type; ``system_prompt`` is the
        system prompt to route on.

    Postconditions:
        Returns the single validated model instance the dummy yielded. Raises
        ``ValueError`` when the routed payload fails ``output_model``
        validation, and ``AssertionError`` when the dummy yields no output.

    Invariants:
        Uses a fresh ``DummyLLMClient`` per call, so per-instance request
        counters never leak between cases.
    """
    assert isinstance(system_prompt, str) and system_prompt.strip(), (
        "system_prompt must be a non-empty string"
    )
    prompt = [{"role": "user", "content": [{"text": serialize_mission(make_mission())}]}]

    async def _collect() -> Any:
        result: Any = None
        async for event in DummyLLMClient().structured_output(
            output_model, prompt, system_prompt=system_prompt
        ):
            result = event["output"]
        return result

    output = asyncio.run(_collect())
    assert output is not None, "DummyLLMClient.structured_output yielded no output event"
    return output


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("_case_id", "factory", "output_model"), _CASES, ids=_CASE_IDS)
def test_dummy_payload_validates_against_agent_schema(
    _case_id: str, factory: Callable[[], Any], output_model: type
) -> None:
    """Each agent's real system prompt routes the dummy to a schema-valid payload.

    Reads ``system_prompt`` off the constructed agent rather than restating it,
    so a prompt reworded in ``agents.py`` is exercised here on the next run.
    """
    agent = factory()
    output = _drive_structured_output(output_model, agent.system_prompt)
    assert isinstance(output, output_model)


@pytest.mark.parametrize(
    ("_case_id", "_factory", "output_model"), _TEXT_ROUTED_CASES, ids=_TEXT_ROUTED_CASE_IDS
)
def test_generic_prompt_payload_is_rejected_by_every_schema(
    _case_id: str, _factory: Callable[[], Any], output_model: type
) -> None:
    """The dummy's unrouted fallback satisfies none of the still-text-routed schemas.

    Without this, ``test_dummy_payload_validates_against_agent_schema`` would
    pass just as happily if an agent's prompt stopped matching its dummy branch
    and fell through — this is what makes that assertion evidence of routing.

    Excludes the six Phase 2 classes in ``_MODEL_ROUTED_CLASS_NAMES``: see
    ``test_model_routed_payload_validates_regardless_of_prompt_text`` for why
    an unrouted prompt is no longer the right probe for those.
    """
    with pytest.raises(ValueError, match="failed to parse into"):
        _drive_structured_output(output_model, _UNROUTED_SYSTEM_PROMPT)


@pytest.mark.parametrize(
    ("_case_id", "_factory", "output_model"), _MODEL_ROUTED_CASES, ids=_MODEL_ROUTED_CASE_IDS
)
def test_model_routed_payload_validates_regardless_of_prompt_text(
    _case_id: str, _factory: Callable[[], Any], output_model: type
) -> None:
    """Phase 2 dummy routing keys off the structured_output model class, not text.

    Proves issue #4252's fix: ``DummyLLMClient.structured_output`` forwards the
    real ``output_model`` class into ``complete_json``, which routes these six
    classes deterministically by class identity (see
    ``_branding_phase2_structured_output_stub``). So even
    ``_UNROUTED_SYSTEM_PROMPT`` — carrying none of the legacy text anchors —
    still yields a schema-valid payload, unlike the remaining classes covered
    by ``test_generic_prompt_payload_is_rejected_by_every_schema``.
    """
    output = _drive_structured_output(output_model, _UNROUTED_SYSTEM_PROMPT)
    assert isinstance(output, output_model)


def test_archetype_stub_matches_the_form_its_prompt_asks_for() -> None:
    """The archetype stub keeps the article its own prompt's examples carry.

    ``BrandArchetype.archetype`` is a free-form ``str``, and the Archetype
    Analyst prompt names its examples as "The Sage, The Creator, The Explorer".
    Stripping the article off the stub would make the no-LLM harness diverge
    from what a real provider returns under that prompt, so the article is the
    contract, not an accident.
    """
    agent = branding_agents.make_archetype_analyst()
    assert "The Sage, The Creator, The Explorer" in agent.system_prompt

    output = _drive_structured_output(BrandArchetypesOutput, agent.system_prompt)
    assert [a.archetype for a in output.brand_archetypes] == ["The Creator"]


def test_every_agent_factory_is_either_covered_or_explicitly_excluded() -> None:
    """No branding agent factory can be added without landing in one of the sets.

    Keeps the contract table from rotting: a new factory fails here until it is
    either given a schema row above or listed as prose-output.
    """
    discovered = {
        name
        for name in dir(branding_agents)
        if name.startswith("make_") and callable(getattr(branding_agents, name))
    }
    assert discovered == _FACTORIES_WITH_STRUCTURED_OUTPUT | _FACTORIES_WITHOUT_STRUCTURED_OUTPUT
    assert not (_FACTORIES_WITH_STRUCTURED_OUTPUT & _FACTORIES_WITHOUT_STRUCTURED_OUTPUT)
