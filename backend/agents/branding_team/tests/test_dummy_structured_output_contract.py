"""Contract tests binding the dummy LLM payloads to the branding agent schemas.

Every branding agent built with ``structured_output=`` hands Strands a Pydantic
model that the provider response *must* validate against. Under
``LLM_PROVIDER=dummy`` that response comes from ``DummyLLMClient.complete_json``
(via ``chat()``/``stream()``/``structured_output()``), which routes the six
Phase 2 "Narrative & Messaging" classes deterministically by the
``structured_output_model``'s class name and routes every other class by
scanning the agent's system prompt for field-name substrings. Nothing else in
the suite drives those two halves against each other, so three independent
edits can silently break the no-LLM harness:

1. tightening a model in ``branding_team.models`` (adding a field, a ``Literal``,
   an enum, or a length bound) past what the dummy stub supplies;
2. editing a dummy payload in ``llm_service.clients.dummy`` so it no longer
   satisfies its model;
3. rewording an agent's system prompt in ``branding_team.agents`` so it stops
   matching its dummy routing branch and falls through to the generic
   ``{"status": ..., "output": ...}`` fallback — this applies only to the
   text-routed classes; the six Phase 2 classes route on class name and are
   unaffected by prompt wording.

All three surface here as a failure naming the offending agent. Case (3) is the
one that needed follow-up commits when Phases 3, 4, and 5 migrated to
``structured_output=``, and ``test_generic_prompt_payload_is_rejected_by_every_schema``
is what keeps the primary assertion honest about catching it — for the
text-routed classes only; the six Phase 2 classes moved to
``test_model_routed_payload_validates_regardless_of_prompt_text`` since an
unrouted prompt no longer breaks them, which is the point of their fix.

This file's scope is deliberately bounded against two neighboring suites so the
three don't re-accumulate the same assertion under different names:

- ``test_dummy_stub_alignment.py`` owns the *strict* positive check (exact
  field-set equality, not just ``model_validate`` not raising) for the Phase 1
  and Phase 2 factories. ``test_dummy_payload_validates_against_agent_schema``
  below skips those 12 cases for that reason — see
  ``_STUB_ALIGNMENT_CASE_IDS`` — and instead covers Phase 3, 4, and 5, which
  that file does not.
- ``llm_service/tests/test_dummy_client.py`` owns ``DummyLLMClient``-internal
  unit tests driven by synthetic prompts, messages, and tool specs. It never
  builds a real ``branding_team.agents.make_*()`` factory or runs a real
  Strands event loop, which is why the negative-path rejection test, the
  archetype phrasing regression, and
  ``test_real_agent_event_loop_routes_deterministically_despite_misleading_prompt``
  stay here — nothing else exercises those real-agent paths.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

import pytest

from branding_team import agents as branding_agents
from branding_team.graphs.phase3_visual import _PHASE3_CONCEPTUALIST_VARIANTS
from branding_team.graphs.shared import build_agent, serialize_mission
from branding_team.models import (
    ApprovalWorkflowsOutput,
    AssetWikiOutput,
    AudienceSegmentsOutput,
    BrandArchetypesOutput,
    BrandArchitectureOutput,
    BrandDiscoveryAudit,
    BrandExperiencePrinciplesOutput,
    BrandGuidelinesOutput,
    BrandHealthKPIsOutput,
    BrandInActionOutput,
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
    ("discovery_auditor", branding_agents.make_discovery_auditor, BrandDiscoveryAudit),
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
    *(
        (
            f"moodboard_conceptualist_{variant.lower()}",
            # Bind the variant in a default arg so each lambda closes over its
            # own value rather than the loop's last one.
            (lambda v=variant: branding_agents.make_moodboard_conceptualist(v)),
            MoodBoardConcept,
        )
        for variant in _PHASE3_CONCEPTUALIST_VARIANTS
    ),
    ("converge_decider", branding_agents.make_converge_decider, CreativeRefinementDecision),
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
        DesignSystemDefinition,
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
    # Phase 5 — Governance & Evolution
    ("ownership_definer", branding_agents.make_ownership_definer, OwnershipOutput),
    (
        "approval_workflow_designer",
        branding_agents.make_approval_workflow_designer,
        ApprovalWorkflowsOutput,
    ),
    ("asset_wiki_planner", branding_agents.make_asset_wiki_planner, AssetWikiOutput),
    ("training_planner", branding_agents.make_training_planner, TrainingOnboardingOutput),
    ("kpi_designer", branding_agents.make_kpi_designer, BrandHealthKPIsOutput),
    ("evolution_framer", branding_agents.make_evolution_framer, EvolutionFrameworkOutput),
    ("brand_rules_codifier", branding_agents.make_brand_rules_codifier, BrandGuidelinesOutput),
)

# Model classes DummyLLMClient.complete_json routes by structured_output_model
# class name (see _branding_structured_output_stub_by_model_name in
# llm_service.clients.dummy) rather than by scanning system-prompt text.
# Phase 2, 4, and 5 output models use this path; for them, an unrouted or
# misleading prompt no longer prevents a valid payload — that is the point of
# issue #4252's fix, and is asserted separately in
# test_model_routed_payload_validates_regardless_of_prompt_text below instead
# of test_generic_prompt_payload_is_rejected_by_every_schema.
_MODEL_ROUTED_CLASS_NAMES: frozenset[str] = frozenset(
    {
        # Phase 2 — Narrative & Messaging
        "BrandStoryOutput",
        "BrandArchetypesOutput",
        "TaglineOutput",
        "MessagingFrameworkOutput",
        "PersonaProfilesOutput",
        "WritingGuidelinesOutput",
        # Phase 4 — Experience & Channel Activation
        "BrandExperiencePrinciplesOutput",
        "ChannelGuidelineOutput",
        "BrandArchitectureOutput",
        "BrandInActionOutput",
        # Phase 5 — Governance & Evolution
        "OwnershipOutput",
        "ApprovalWorkflowsOutput",
        "AssetWikiOutput",
        "TrainingOnboardingOutput",
        "BrandHealthKPIsOutput",
        "EvolutionFrameworkOutput",
        "BrandGuidelinesOutput",
    }
)
# Structured-output models with every field optional/default-constructible —
# BrandDiscoveryAudit, CreativeRefinementDecision, and DesignSystemDefinition,
# each collapsed (Story 3b) to a single soft model used both as its agent's
# structured_output and as the corresponding phase output's default_factory
# merge target, rather than split into a strict agent-facing twin. The
# dummy's generic unrouted fallback payload validates against these just as
# happily as a routed one, so they can't serve as routing evidence for
# test_generic_prompt_payload_is_rejected_by_every_schema either — excluded
# here for an analogous reason to the model-routed classes above, via a
# different mechanism (schema permissiveness, not routing).
_PERMISSIVE_CLASS_NAMES: frozenset[str] = frozenset(
    {"BrandDiscoveryAudit", "CreativeRefinementDecision", "DesignSystemDefinition"}
)

# Case ids whose "does the dummy stub validate against the real schema"
# property is already asserted — more strongly, via exact field-set equality
# rather than a plain isinstance check — by
# test_dummy_stub_alignment.py::test_dummy_stub_matches_agent_output_model.
# Excluded only from the positive-validation parametrization below; they
# remain in _CASES so _TEXT_ROUTED_CASES/_MODEL_ROUTED_CASES (used by the
# rejection and model-routing tests) stay complete.
_STUB_ALIGNMENT_CASE_IDS: frozenset[str] = frozenset(
    {
        "discovery_auditor",
        "purpose_vision_writer",
        "values_articulator",
        "audience_segmenter",
        "differentiation_mapper",
        "positioning_synthesizer",
        "storyteller",
        "archetype_analyst",
        "tagline_writer",
        "message_mapper",
        "persona_builder",
        "voice_principles_drafter",
    }
)
_SCHEMA_VALIDATION_CASES: tuple[tuple[str, Callable[[], Any], type], ...] = tuple(
    case for case in _CASES if case[0] not in _STUB_ALIGNMENT_CASE_IDS
)
_SCHEMA_VALIDATION_CASE_IDS: tuple[str, ...] = tuple(
    case_id for case_id, _factory, _model in _SCHEMA_VALIDATION_CASES
)

_TEXT_ROUTED_CASES: tuple[tuple[str, Callable[[], Any], type], ...] = tuple(
    case
    for case in _CASES
    if case[2].__name__ not in _MODEL_ROUTED_CLASS_NAMES
    and case[2].__name__ not in _PERMISSIVE_CLASS_NAMES
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
_MODEL_ROUTED_CLASSES: tuple[type, ...] = tuple(
    model for _case_id, _factory, model in _MODEL_ROUTED_CASES
)
_MODEL_ROUTED_CLASS_NAME_IDS: tuple[str, ...] = tuple(cls.__name__ for cls in _MODEL_ROUTED_CLASSES)

# Factory names covered by ``_CASES``. Parameterized factories appear once here
# and expand to several table rows, so this is not derivable from ``_CASES``'
# case ids.
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
        "make_ownership_definer",
        "make_approval_workflow_designer",
        "make_asset_wiki_planner",
        "make_training_planner",
        "make_kpi_designer",
        "make_evolution_framer",
        "make_brand_rules_codifier",
    }
)

# All branding agent factories now use ``structured_output=`` (Phases 1-5).
# Kept as an explicit (currently empty) set — rather than removing it and the
# union check below — so a future phase or team addition that ships without a
# schema has an obvious place to be listed instead of silently expanding
# ``_FACTORIES_WITH_STRUCTURED_OUTPUT``'s assumptions.
_FACTORIES_WITHOUT_STRUCTURED_OUTPUT: frozenset[str] = frozenset()


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


@pytest.mark.parametrize(
    ("_case_id", "factory", "output_model"),
    _SCHEMA_VALIDATION_CASES,
    ids=_SCHEMA_VALIDATION_CASE_IDS,
)
def test_dummy_payload_validates_against_agent_schema(
    _case_id: str, factory: Callable[[], Any], output_model: type
) -> None:
    """Each agent's real system prompt routes the dummy to a schema-valid payload.

    Reads ``system_prompt`` off the constructed agent rather than restating it,
    so a prompt reworded in ``agents.py`` is exercised here on the next run.

    Covers Phase 3, 4, and 5 only — the Phase 1/2 factories in
    ``_STUB_ALIGNMENT_CASE_IDS`` get a strictly stronger check (exact
    field-set equality) from
    ``test_dummy_stub_alignment.py::test_dummy_stub_matches_agent_output_model``
    instead, so asserting the weaker ``isinstance`` here too would be
    redundant CI coverage of the same property.
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


@pytest.mark.parametrize("output_model", _MODEL_ROUTED_CLASSES, ids=_MODEL_ROUTED_CLASS_NAME_IDS)
def test_real_agent_event_loop_routes_deterministically_despite_misleading_prompt(
    output_model: type,
) -> None:
    """The actual production path — a real Strands event loop, not
    ``DummyLLMClient.structured_output()`` called directly — must also route
    deterministically, for every Phase 2 class the dispatcher covers.

    Every other test in this module drives ``DummyLLMClient.structured_output()``
    directly via ``_drive_structured_output``. That method is only reachable
    through Strands' deprecated ``Agent.structured_output()``/
    ``structured_output_async()``, which nothing in this repo calls. The
    current API (``structured_output_model=``, what ``build_agent`` uses)
    drives agents through the tool-calling event loop instead — Strands
    registers a ``StructuredOutputTool`` and the loop calls
    ``Model.stream()``, which for a real ``Agent`` always lands on
    ``LLMClientModel.stream()`` -> ``chat()``, never on ``.structured_output()``.
    So a passing suite of ``_drive_structured_output``-based tests does not by
    itself prove the real path routes correctly — this test does, by actually
    running the event loop with a system prompt that carries none of the
    legacy text anchors. Parametrized over all six routed classes rather than
    just ``TaglineOutput``: the dispatcher is a flat name lookup shared by all
    six, but a class-specific edge case on the event-loop path would
    otherwise regress silently for the other five.
    """
    agent = build_agent(
        name="RealEventLoopProbe",
        system_prompt=_UNROUTED_SYSTEM_PROMPT,
        structured_output=output_model,
    )
    result = agent("Please respond.")
    assert isinstance(result.structured_output, output_model)


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
