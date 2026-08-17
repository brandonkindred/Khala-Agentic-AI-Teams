"""Drift guard: cardinality constants are the single source for schema + prompt.

Each numeric list/dict cardinality in ``models.py`` (e.g. "3-5 core values") is
stated in two places that must agree: the Pydantic ``Field(min_length=...,
max_length=...)`` on the agent-output model, and the prompt prose in
``agents.py`` that tells the LLM how many items to produce. Both now interpolate
the same named constant from ``models.py`` so they cannot silently desync.

These tests are the executable form of that invariant. For every migrated
constraint they assert that:

* the model field's ``min_length``/``max_length`` equal the named constant, and
* the rendered agent ``system_prompt`` contains the same ``"{min}-{max}"`` range.

Both assertions read the value from the imported constant, so re-hardcoding a
divergent number in either the schema or the prompt fails here. This is the
systemic sibling of the single-instance prompt/schema drift guards already in
``test_models.py`` and ``test_agents.py``.
"""

from __future__ import annotations

from typing import Callable

import annotated_types as at
import pytest
from pydantic import BaseModel
from strands import Agent

from branding_team import models as m
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
    make_differentiation_mapper,
    make_kpi_designer,
    make_message_mapper,
    make_persona_builder,
    make_photography_video_director,
    make_social_guide,
    make_storyteller,
    make_training_planner,
    make_typography_builder,
    make_values_articulator,
    make_voice_principles_drafter,
    make_voice_tone_builder,
)

# Each row: (label, model class owning the field, field name, MIN const, MAX
# const, factory whose prompt describes the constraint). ``label`` keeps pytest
# ids readable. Factories are shared where one agent produces several
# constrained fields.
_RANGE_CASES: list[tuple[str, type[BaseModel], str, int, int, Callable[[], Agent]]] = [
    # Phase 1 — Strategic Core
    (
        "core_values",
        m.CoreValuesOutput,
        "core_values",
        m.CORE_VALUES_MIN,
        m.CORE_VALUES_MAX,
        make_values_articulator,
    ),
    (
        "audience_segments",
        m.AudienceSegmentsOutput,
        "target_audience_segments",
        m.AUDIENCE_SEGMENTS_MIN,
        m.AUDIENCE_SEGMENTS_MAX,
        make_audience_segmenter,
    ),
    (
        "differentiation_pillars",
        m.DifferentiationPillarsOutput,
        "differentiation_pillars",
        m.DIFFERENTIATION_PILLARS_MIN,
        m.DIFFERENTIATION_PILLARS_MAX,
        make_differentiation_mapper,
    ),
    # Phase 2 — Narrative & Messaging
    (
        "brand_archetypes",
        m.BrandArchetypesOutput,
        "brand_archetypes",
        m.BRAND_ARCHETYPES_MIN,
        m.BRAND_ARCHETYPES_MAX,
        make_archetype_analyst,
    ),
    (
        "messaging_framework",
        m.MessagingFrameworkOutput,
        "messaging_framework",
        m.MESSAGING_PILLARS_MIN,
        m.MESSAGING_PILLARS_MAX,
        make_message_mapper,
    ),
    (
        "persona_profiles",
        m.PersonaProfilesOutput,
        "persona_profiles",
        m.PERSONA_PROFILES_MIN,
        m.PERSONA_PROFILES_MAX,
        make_persona_builder,
    ),
    (
        "voice_principles",
        m.WritingGuidelinesBody,
        "voice_principles",
        m.VOICE_PRINCIPLES_MIN,
        m.VOICE_PRINCIPLES_MAX,
        make_voice_principles_drafter,
    ),
    (
        "style_dos",
        m.WritingGuidelinesBody,
        "style_dos",
        m.STYLE_DOS_MIN,
        m.STYLE_DOS_MAX,
        make_voice_principles_drafter,
    ),
    (
        "style_donts",
        m.WritingGuidelinesBody,
        "style_donts",
        m.STYLE_DONTS_MIN,
        m.STYLE_DONTS_MAX,
        make_voice_principles_drafter,
    ),
    (
        "editorial_quality_bar",
        m.WritingGuidelinesBody,
        "editorial_quality_bar",
        m.EDITORIAL_QUALITY_BAR_MIN,
        m.EDITORIAL_QUALITY_BAR_MAX,
        make_voice_principles_drafter,
    ),
    # Phase 3 — Visual & Expressive Identity
    (
        "color_palette",
        m.ColorPaletteSystemOutput,
        "color_palette",
        m.COLOR_PALETTE_MIN,
        m.COLOR_PALETTE_MAX,
        make_color_system_builder,
    ),
    (
        "typography_system",
        m.TypographySystemOutput,
        "typography_system",
        m.TYPOGRAPHY_SYSTEM_MIN,
        m.TYPOGRAPHY_SYSTEM_MAX,
        make_typography_builder,
    ),
    (
        "motion_principles",
        m.PhotographyVideoOutput,
        "motion_principles",
        m.MOTION_PRINCIPLES_MIN,
        m.MOTION_PRINCIPLES_MAX,
        make_photography_video_director,
    ),
    (
        "language_dos",
        m.VoiceToneOutput,
        "language_dos",
        m.LANGUAGE_DOS_MIN,
        m.LANGUAGE_DOS_MAX,
        make_voice_tone_builder,
    ),
    (
        "language_donts",
        m.VoiceToneOutput,
        "language_donts",
        m.LANGUAGE_DONTS_MIN,
        m.LANGUAGE_DONTS_MAX,
        make_voice_tone_builder,
    ),
    # Phase 4 — Experience & Channel Activation
    (
        "channel_dos",
        m.ChannelGuidelineOutput,
        "dos",
        m.CHANNEL_DOS_MIN,
        m.CHANNEL_DOS_MAX,
        make_social_guide,
    ),
    (
        "channel_donts",
        m.ChannelGuidelineOutput,
        "donts",
        m.CHANNEL_DONTS_MIN,
        m.CHANNEL_DONTS_MAX,
        make_social_guide,
    ),
    (
        "channel_content_types",
        m.ChannelGuidelineOutput,
        "content_types",
        m.CHANNEL_CONTENT_TYPES_MIN,
        m.CHANNEL_CONTENT_TYPES_MAX,
        make_social_guide,
    ),
    (
        "brand_experience_principles",
        m.BrandExperiencePrinciplesOutput,
        "brand_experience_principles",
        m.BRAND_EXPERIENCE_PRINCIPLES_MIN,
        m.BRAND_EXPERIENCE_PRINCIPLES_MAX,
        make_brand_experience_principler,
    ),
    (
        "signature_moments",
        m.BrandExperiencePrinciplesOutput,
        "signature_moments",
        m.SIGNATURE_MOMENTS_MIN,
        m.SIGNATURE_MOMENTS_MAX,
        make_brand_experience_principler,
    ),
    (
        "sensory_elements",
        m.BrandExperiencePrinciplesOutput,
        "sensory_elements",
        m.SENSORY_ELEMENTS_MIN,
        m.SENSORY_ELEMENTS_MAX,
        make_brand_experience_principler,
    ),
    (
        "naming_conventions",
        m.BrandArchitectureOutput,
        "naming_conventions",
        m.NAMING_CONVENTIONS_MIN,
        m.NAMING_CONVENTIONS_MAX,
        make_brand_architecture_builder,
    ),
    (
        "terminology_glossary",
        m.BrandArchitectureOutput,
        "terminology_glossary",
        m.TERMINOLOGY_GLOSSARY_MIN,
        m.TERMINOLOGY_GLOSSARY_MAX,
        make_brand_architecture_builder,
    ),
    (
        "brand_in_action",
        m.BrandInActionOutput,
        "brand_in_action",
        m.BRAND_IN_ACTION_MIN,
        m.BRAND_IN_ACTION_MAX,
        make_brand_in_action_illustrator,
    ),
    # Phase 5 — Governance & Evolution
    (
        "approval_workflows",
        m.ApprovalWorkflowsOutput,
        "approval_workflows",
        m.APPROVAL_WORKFLOWS_MIN,
        m.APPROVAL_WORKFLOWS_MAX,
        make_approval_workflow_designer,
    ),
    (
        "agency_briefing_protocols",
        m.ApprovalWorkflowsOutput,
        "agency_briefing_protocols",
        m.AGENCY_BRIEFING_PROTOCOLS_MIN,
        m.AGENCY_BRIEFING_PROTOCOLS_MAX,
        make_approval_workflow_designer,
    ),
    (
        "asset_management_guidance",
        m.AssetWikiOutput,
        "asset_management_guidance",
        m.ASSET_MANAGEMENT_GUIDANCE_MIN,
        m.ASSET_MANAGEMENT_GUIDANCE_MAX,
        make_asset_wiki_planner,
    ),
    (
        "wiki_backlog",
        m.AssetWikiOutput,
        "wiki_backlog",
        m.WIKI_BACKLOG_MIN,
        m.WIKI_BACKLOG_MAX,
        make_asset_wiki_planner,
    ),
    (
        "training_onboarding_plan",
        m.TrainingOnboardingOutput,
        "training_onboarding_plan",
        m.TRAINING_ONBOARDING_MIN,
        m.TRAINING_ONBOARDING_MAX,
        make_training_planner,
    ),
    (
        "brand_health_kpis",
        m.BrandHealthKPIsOutput,
        "brand_health_kpis",
        m.BRAND_HEALTH_KPIS_MIN,
        m.BRAND_HEALTH_KPIS_MAX,
        make_kpi_designer,
    ),
    (
        "review_trigger_points",
        m.BrandHealthKPIsOutput,
        "review_trigger_points",
        m.REVIEW_TRIGGER_POINTS_MIN,
        m.REVIEW_TRIGGER_POINTS_MAX,
        make_kpi_designer,
    ),
    (
        "brand_guidelines",
        m.BrandGuidelinesOutput,
        "brand_guidelines",
        m.BRAND_GUIDELINES_MIN,
        m.BRAND_GUIDELINES_MAX,
        make_brand_rules_codifier,
    ),
]


def _field_bounds(model_cls: type[BaseModel], field: str) -> tuple[int, int]:
    """Return the ``(min_length, max_length)`` a Pydantic field enforces.

    Preconditions:
        ``field`` names a field on ``model_cls`` carrying both an
        ``annotated_types.MinLen`` and a ``MaxLen`` in its metadata.
    Postconditions:
        Returns the two bounds as ints.
    """
    md = model_cls.model_fields[field].metadata
    mn = next((c.min_length for c in md if isinstance(c, at.MinLen)), None)
    mx = next((c.max_length for c in md if isinstance(c, at.MaxLen)), None)
    if mn is None or mx is None:
        raise AssertionError(f"{model_cls.__name__}.{field} missing MinLen/MaxLen metadata")
    return mn, mx


@pytest.mark.parametrize(
    "model_cls, field, min_const, max_const",
    [(c[1], c[2], c[3], c[4]) for c in _RANGE_CASES],
    ids=[c[0] for c in _RANGE_CASES],
)
def test_schema_constraint_uses_named_constant(
    model_cls: type[BaseModel], field: str, min_const: int, max_const: int
) -> None:
    """The Pydantic ``Field`` bounds equal the named constant (schema side)."""
    assert _field_bounds(model_cls, field) == (min_const, max_const)


@pytest.mark.parametrize(
    "factory, min_const, max_const",
    [(c[5], c[3], c[4]) for c in _RANGE_CASES],
    ids=[c[0] for c in _RANGE_CASES],
)
def test_prompt_renders_named_constant_range(
    factory: Callable[[], Agent], min_const: int, max_const: int
) -> None:
    """The rendered agent prompt states the constant's ``{min}-{max}`` range (prompt side)."""
    assert f"{min_const}-{max_const}" in factory().system_prompt


def test_boilerplate_variants_fixed_count_single_sourced() -> None:
    """The fixed ``boilerplate_variants`` count is single-sourced across schema and prompt."""
    assert _field_bounds(m.BrandStoryOutput, "boilerplate_variants") == (
        m.BOILERPLATE_VARIANTS_COUNT,
        m.BOILERPLATE_VARIANTS_COUNT,
    )
    assert f"{m.BOILERPLATE_VARIANTS_COUNT} versions" in make_storyteller().system_prompt
