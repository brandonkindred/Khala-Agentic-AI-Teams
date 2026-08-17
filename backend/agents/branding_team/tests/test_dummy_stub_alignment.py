"""Regression tests: dummy-client branding stubs match their agents' ``*Output`` models.

Guards against the class of bug described by the automated review that flagged
``dummy.py``'s Phase 1/2 branding heuristics as a potential cumulative-superset
mismatch against strict per-agent ``structured_output=`` models. Each Phase 1
(Strategic Core) and Phase 2 (Narrative & Messaging) agent's dummy stub is
exercised through its *real* ``system_prompt`` and validated against its *real*
``structured_output=`` model, asserting an exact field-set match — not just
that ``model_validate`` happens not to raise (which would pass silently even on
a true superset, since pydantic's default is ``extra="ignore"``).
"""

from __future__ import annotations

from typing import Callable

import pytest
from pydantic import BaseModel
from strands import Agent

from branding_team.agents import (
    make_archetype_analyst,
    make_audience_segmenter,
    make_differentiation_mapper,
    make_discovery_auditor,
    make_message_mapper,
    make_persona_builder,
    make_positioning_synthesizer,
    make_purpose_vision_writer,
    make_storyteller,
    make_tagline_writer,
    make_values_articulator,
    make_voice_principles_drafter,
)
from branding_team.models import (
    AudienceSegmentsOutput,
    BrandArchetypesOutput,
    BrandDiscoveryAudit,
    BrandStoryOutput,
    CoreValuesOutput,
    DifferentiationPillarsOutput,
    MessagingFrameworkOutput,
    PersonaProfilesOutput,
    PositioningOutput,
    PurposeVisionOutput,
    TaglineOutput,
    WritingGuidelinesOutput,
)
from branding_team.tests.conftest import make_mission
from llm_service import DummyLLMClient

# (factory, expected structured_output model) — mirrors the pairing declared
# in each factory's ``build_agent(..., structured_output=...)`` call in
# ``agents.py``.
_PHASE1_AND_PHASE2_CASES: list[tuple[Callable[[], Agent], type[BaseModel]]] = [
    (make_discovery_auditor, BrandDiscoveryAudit),
    (make_purpose_vision_writer, PurposeVisionOutput),
    (make_values_articulator, CoreValuesOutput),
    (make_audience_segmenter, AudienceSegmentsOutput),
    (make_differentiation_mapper, DifferentiationPillarsOutput),
    (make_positioning_synthesizer, PositioningOutput),
    (make_storyteller, BrandStoryOutput),
    (make_archetype_analyst, BrandArchetypesOutput),
    (make_tagline_writer, TaglineOutput),
    (make_message_mapper, MessagingFrameworkOutput),
    (make_persona_builder, PersonaProfilesOutput),
    (make_voice_principles_drafter, WritingGuidelinesOutput),
]


@pytest.mark.parametrize(
    "factory,output_model",
    _PHASE1_AND_PHASE2_CASES,
    ids=[factory.__name__ for factory, _ in _PHASE1_AND_PHASE2_CASES],
)
def test_dummy_stub_matches_agent_output_model(
    factory: Callable[[], Agent], output_model: type[BaseModel]
) -> None:
    agent = factory()
    prompt = make_mission().model_dump_json(indent=2)

    result = DummyLLMClient().complete_json(
        prompt,
        system_prompt=agent.system_prompt,
        structured_output_model=output_model,
    )

    assert isinstance(result, dict)
    output_model.model_validate(result)
    assert set(result.keys()) == set(output_model.model_fields.keys()), (
        f"{factory.__name__}: dummy stub returned {sorted(result.keys())}, "
        f"{output_model.__name__} declares {sorted(output_model.model_fields.keys())}"
    )
