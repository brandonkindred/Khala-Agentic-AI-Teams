"""Models for the branding strategy multi-agent team.

Implements a 5-phase brand development framework:
  Phase 1 — Strategic Core
  Phase 2 — Narrative & Messaging
  Phase 3 — Visual & Expressive Identity
  Phase 4 — Experience & Channel Activation
  Phase 5 — Governance & Evolution

Collapsed nested-item pattern
-----------------------------
Nested item models (canonical pair: ``CoreValue`` / ``CoreValueOutput``) are a
soft merge-target base plus a ``_derive_strict_variant``-generated strict
subclass that redeclares only the fields that must be required/non-empty.
Field lists are not duplicated. The soft base is the merge target for partial
per-agent fragments, ``default_factory`` construction, and permissive
fixtures. The strict subclass is the Strands ``structured_output=`` schema so
a blank or incomplete LLM payload fails validation and retries instead of
silently producing empty content.

``model_validate(..., context={"strict": True})`` is not used: Strands
constructs structured-output instances itself and does not thread a
``context=`` kwarg through any call site this package controls. A derived
subclass enforces constraints at the type level regardless of how it is
instantiated. ``isinstance(CoreValueOutput(...), CoreValue)`` holds because
the generated class is a real subclass of the soft base.

This pattern does not apply to the remaining hand-written sibling pair that
was never collapsed (``ChannelGuideline`` / ``ChannelGuidelineOutput``).
``BrandDiscoveryAudit``, ``MoodBoardConcept``, ``CreativeRefinementDecision``,
and ``DesignSystemDefinition`` were each fully collapsed to a single model
(used both as their agent's ``structured_output`` and as the corresponding
phase output's ``default_factory`` merge target) rather than split into this
soft/strict pair, since a no-argument-constructible default is needed either
way. The Phase 2 specialist models (``BrandStoryOutput`` … ``WritingGuidelinesOutput``)
used to be a cumulative-inheritance chain, exempted from this pattern; Story
5b Step 1 flattened them into six independent own-field models instead — see
each class's docstring.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Dict, List, Optional

from pydantic import BaseModel, Field, create_model

from shared.hitl.models import HumanReview as HumanReview  # noqa: F401 — re-export

# Individual list/dict items, not just container length, must be non-empty —
# a fully populated ``List[str] = Field(min_length=N)`` still accepts N blank
# strings, undermining every "requires non-empty content" docstring below.
NonEmptyStr = Annotated[str, Field(min_length=1)]

# ---------------------------------------------------------------------------
# Cardinality constants — single source for list/dict length constraints
# ---------------------------------------------------------------------------
# Each constraint below is the SOLE source of truth for a cardinality that is
# otherwise stated twice: once in a Pydantic ``Field(min_length=..., max_length=...)``
# on an agent-output model in this file, and once in the corresponding prompt
# prose in ``agents.py`` (e.g. "3-5 core values"). Both sites interpolate the
# same constant, so a schema edit and a prompt edit can no longer silently
# desync. Constraints that happen to share a value (several ``(3, 4)`` pairs)
# still get their own named constant: they are independent limits that coincide
# today, and changing one must not change the others.
#
# Fixed-count constraints where ``min == max`` (a list of exactly N items) use a
# single ``*_COUNT`` constant fed to both ``min_length`` and ``max_length``.

# Phase 1 — Strategic Core
CORE_VALUES_MIN = 3
CORE_VALUES_MAX = 5
AUDIENCE_SEGMENTS_MIN = 1
AUDIENCE_SEGMENTS_MAX = 3
DIFFERENTIATION_PILLARS_MIN = 2
DIFFERENTIATION_PILLARS_MAX = 4

# Phase 2 — Narrative & Messaging
BOILERPLATE_VARIANTS_COUNT = 3
ELEVATOR_PITCHES_COUNT = 3
BRAND_ARCHETYPES_MIN = 1
BRAND_ARCHETYPES_MAX = 2
MESSAGING_PILLARS_MIN = 3
MESSAGING_PILLARS_MAX = 4
PERSONA_PROFILES_MIN = 2
PERSONA_PROFILES_MAX = 3
VOICE_PRINCIPLES_MIN = 3
VOICE_PRINCIPLES_MAX = 4
STYLE_DOS_MIN = 3
STYLE_DOS_MAX = 4
STYLE_DONTS_MIN = 3
STYLE_DONTS_MAX = 4
EDITORIAL_QUALITY_BAR_MIN = 3
EDITORIAL_QUALITY_BAR_MAX = 4

# Phase 3 — Visual & Expressive Identity
COLOR_PALETTE_MIN = 5
COLOR_PALETTE_MAX = 7
TYPOGRAPHY_SYSTEM_MIN = 3
TYPOGRAPHY_SYSTEM_MAX = 4
MOTION_PRINCIPLES_MIN = 3
MOTION_PRINCIPLES_MAX = 4
LANGUAGE_DOS_MIN = 4
LANGUAGE_DOS_MAX = 5
LANGUAGE_DONTS_MIN = 4
LANGUAGE_DONTS_MAX = 5

# Phase 4 — Experience & Channel Activation
CHANNEL_DOS_MIN = 3
CHANNEL_DOS_MAX = 4
CHANNEL_DONTS_MIN = 3
CHANNEL_DONTS_MAX = 4
CHANNEL_CONTENT_TYPES_MIN = 3
CHANNEL_CONTENT_TYPES_MAX = 5
BRAND_EXPERIENCE_PRINCIPLES_MIN = 3
BRAND_EXPERIENCE_PRINCIPLES_MAX = 5
SIGNATURE_MOMENTS_MIN = 3
SIGNATURE_MOMENTS_MAX = 5
SENSORY_ELEMENTS_MIN = 2
SENSORY_ELEMENTS_MAX = 4
NAMING_CONVENTIONS_MIN = 3
NAMING_CONVENTIONS_MAX = 5
TERMINOLOGY_GLOSSARY_MIN = 5
TERMINOLOGY_GLOSSARY_MAX = 10
BRAND_IN_ACTION_MIN = 3
BRAND_IN_ACTION_MAX = 5

# Phase 5 — Governance & Evolution
APPROVAL_WORKFLOWS_MIN = 3
APPROVAL_WORKFLOWS_MAX = 5
AGENCY_BRIEFING_PROTOCOLS_MIN = 3
AGENCY_BRIEFING_PROTOCOLS_MAX = 5
ASSET_MANAGEMENT_GUIDANCE_MIN = 3
ASSET_MANAGEMENT_GUIDANCE_MAX = 5
WIKI_BACKLOG_MIN = 4
WIKI_BACKLOG_MAX = 6
TRAINING_ONBOARDING_MIN = 4
TRAINING_ONBOARDING_MAX = 6
BRAND_HEALTH_KPIS_MIN = 4
BRAND_HEALTH_KPIS_MAX = 6
REVIEW_TRIGGER_POINTS_MIN = 3
REVIEW_TRIGGER_POINTS_MAX = 5
BRAND_GUIDELINES_MIN = 5
BRAND_GUIDELINES_MAX = 8

# ---------------------------------------------------------------------------
# Strict/soft derived-subclass pattern (see module docstring)
# ---------------------------------------------------------------------------
# Nested item models use ``_derive_strict_variant`` immediately below the
# soft class they derive from. See ``CoreValue``/``CoreValueOutput``.


def _derive_strict_variant(
    name: str,
    base: type[BaseModel],
    /,
    *,
    doc: str,
    **field_overrides: Any,
) -> type[BaseModel]:
    """Derive a strict agent-output twin from a soft merge-target model.

    Generates ``name`` as a real ``pydantic.BaseModel`` subclass of ``base``
    via ``pydantic.create_model``, re-declaring only the fields named in
    ``field_overrides`` with stricter annotations/constraints; every other
    field is inherited unchanged from ``base``. Used instead of a
    ``context=``-gated ``field_validator`` because the strict twin's
    primary caller — the Strands SDK's own ``structured_output`` parsing
    of the LLM's tool-call JSON — constructs instances directly and does
    not thread any ``context=`` kwarg through call sites we control; a
    derived-subclass twin enforces its constraints independent of how or
    where it is instantiated. Note the returned class is a genuine
    subclass of ``base`` (``issubclass`` / ``isinstance`` against ``base``
    hold), not an independent sibling type. ``name``/``base`` are
    positional-only so a field literally named ``name`` or ``base`` (e.g.
    ``AudienceSegment.name``, ``PersonaProfile.name``) can still be passed
    as a ``field_overrides`` keyword without colliding with these
    parameters.

    Preconditions:
        - ``base`` is a concrete ``pydantic.BaseModel`` subclass whose own
          fields are all constructible with defaults (this file's soft
          merge-target contract).
        - Each value in ``field_overrides`` is a ``(annotation, FieldInfo)``
          pair, keyed by a field name that already exists on ``base``;
          this function only redeclares existing fields, it does not add
          new ones.
    Postconditions:
        - Returns a new class, named ``name``, that is a subclass of both
          ``base`` and ``pydantic.BaseModel`` — usable anywhere a
          ``BaseModel`` subclass is expected, including direct
          construction (``ClassName(**kwargs)``), ``.model_validate``,
          ``List[<returned class>]`` field annotations elsewhere in this
          module, and ``.model_json_schema()``.
        - Every field named in ``field_overrides`` enforces the given
          stricter annotation/constraints (a field with a default on
          ``base`` may become required with no default on the returned
          class); every other field keeps ``base``'s original annotation,
          default, and constraints.
        - Does not mutate ``base``; ``base`` keeps accepting its original,
          more permissive field values.
    """
    return create_model(
        name, __base__=base, __module__=base.__module__, __doc__=doc, **field_overrides
    )


# Common closing sentence for every strict-twin ``doc=`` below — factored out
# so the 17 call sites don't each hand-duplicate the same boilerplate tail.
_STRICT_TWIN_DOC_SUFFIX = (
    "Generated via ``_derive_strict_variant`` — see that helper's docstring "
    "for the shared strict/soft twin pattern this file uses."
)


# ---------------------------------------------------------------------------
# Shared models
# ---------------------------------------------------------------------------


class Client(BaseModel):
    """Agency client; one client owns many brands."""

    id: str
    name: str = Field(..., min_length=1)
    created_at: str = ""
    updated_at: str = ""
    contact_info: Optional[str] = None
    notes: Optional[str] = None


class BrandStatus(str, Enum):
    draft = "draft"
    active = "active"
    evolving = "evolving"
    archived = "archived"


class WorkflowStatus(str, Enum):
    """Branding-run lifecycle status.

    Intentionally team-local (not in ``shared.hitl``): the terminal
    ``READY_FOR_ROLLOUT`` is branding-specific. Only the string value
    ``needs_human_decision`` overlaps other teams' enums. See
    ``backend/shared/hitl/README.md`` ("Non-shared: team WorkflowStatus")
    for the full cross-team decision record.
    """

    NEEDS_HUMAN_DECISION = "needs_human_decision"
    READY_FOR_ROLLOUT = "ready_for_rollout"


class BrandPhase(str, Enum):
    """Which phase the brand is currently in."""

    STRATEGIC_CORE = "strategic_core"
    NARRATIVE_MESSAGING = "narrative_messaging"
    VISUAL_IDENTITY = "visual_identity"
    CHANNEL_ACTIVATION = "channel_activation"
    GOVERNANCE = "governance"
    COMPLETE = "complete"


class PhaseGateStatus(str, Enum):
    """Gate status for phase transitions."""

    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REVISION_REQUESTED = "revision_requested"


class ColorPalette(BaseModel):
    """A candidate color palette for brand identity selection."""

    name: str = ""
    description: str = ""
    colors: List[str] = Field(default_factory=list)
    sentiment: str = ""  # e.g. "warm and energetic", "cool and professional"


# Sentinel strings for mission fields that have no real value yet.
# Used by default-mission construction and placeholder detection.
MISSION_PLACEHOLDER_TBD = "TBD"
MISSION_PLACEHOLDER_TO_BE_DISCUSSED = "To be discussed."
MISSION_PLACEHOLDERS = (
    MISSION_PLACEHOLDER_TBD,
    MISSION_PLACEHOLDER_TO_BE_DISCUSSED,
    "—",
    "",
)


class BrandingMissionFields(BaseModel):
    """Shared required/defaulted mission fields for branding domain + future API DTOs.

    Preconditions:
        - ``company_name`` length >= 2
        - ``company_description`` length >= 10
        - ``target_audience`` length >= 3
    Postconditions:
        - Instance exposes the eight shared mission fields with the defaults
          declared below when optional inputs are omitted.
    """

    company_name: str = Field(..., min_length=2)
    company_description: str = Field(..., min_length=10)
    target_audience: str = Field(..., min_length=3)
    values: List[str] = Field(default_factory=list)
    differentiators: List[str] = Field(default_factory=list)
    desired_voice: str = "clear, confident, human"
    existing_brand_material: List[str] = Field(default_factory=list)
    wiki_path: Optional[str] = None

    def mission_fields(self) -> dict[str, Any]:
        """Return only the eight shared mission fields as a plain dict.

        Preconditions:
            ``self`` is a valid ``BrandingMissionFields`` instance (or subclass).
        Postconditions:
            Returns a dict whose keys are exactly the eight shared mission field
            names from ``BrandingMissionFields.model_fields``; values match
            ``self``; API-only extras declared on subclasses are omitted.
        """
        return self.model_dump(include=set(BrandingMissionFields.model_fields))


class BrandingMission(BrandingMissionFields):
    """Full branding mission: shared fields + visual-identity inputs.

    Preconditions:
        - Same as ``BrandingMissionFields`` for the shared required strings.
    Postconditions:
        - Instance exposes shared mission fields plus visual-identity fields;
          omitted visual fields use the defaults declared below.
    """

    # Visual identity fields — populated during guided palette selection
    color_inspiration: List[str] = Field(default_factory=list)
    color_palettes: List[ColorPalette] = Field(default_factory=list)
    selected_palette_index: Optional[int] = None
    visual_style: str = ""  # e.g. "minimalist", "maximalist", "editorial"
    typography_preference: str = ""  # e.g. "geometric sans-serif", "humanist serif"
    interface_density: str = ""  # e.g. "spacious/minimalist", "dense/information-rich"


# ---------------------------------------------------------------------------
# Phase 1 — Strategic Core
# ---------------------------------------------------------------------------


class CoreValue(BaseModel):
    """A brand value with behavioral definition."""

    value: str
    behavioral_definition: str = ""
    observable_behaviors: List[str] = Field(default_factory=list)


CoreValueOutput = _derive_strict_variant(
    "CoreValueOutput",
    CoreValue,
    doc=(
        "Agent-facing core value; requires non-empty fields.\n\n"
        "Field-for-field twin of ``CoreValue`` with required content, matching "
        "the Phase 3 nested-output-model pattern (``LogoUsageRuleOutput``, "
        "``ColorEntryOutput``, ``TypographySpecOutput``, ``VoiceToneEntryOutput``, "
        "``BrandArchitectureRuleOutput``, ``PersonaProfileOutput``, "
        "``MessagingPillarOutput``, ``ElevatorPitchOutput``, ``BrandArchetypeOutput``, "
        "``DifferentiationPillarOutput``) — ``CoreValue`` itself must stay soft "
        "(only ``value`` required) since it also backs "
        "``StrategicCoreOutput.core_values``'s merge target. "
    )
    + _STRICT_TWIN_DOC_SUFFIX,
    value=(str, Field(min_length=1)),
    behavioral_definition=(str, Field(min_length=1)),
    observable_behaviors=(List[NonEmptyStr], Field(min_length=1)),
)


class AudienceSegment(BaseModel):
    """A target audience segment with psychographic detail."""

    name: str
    description: str = ""
    pain_points: List[str] = Field(default_factory=list)
    goals: List[str] = Field(default_factory=list)
    decision_drivers: List[str] = Field(default_factory=list)


AudienceSegmentOutput = _derive_strict_variant(
    "AudienceSegmentOutput",
    AudienceSegment,
    doc=(
        "Agent-facing audience segment; requires non-empty fields.\n\n"
        "Field-for-field twin of ``AudienceSegment`` with required content, "
        "matching the Phase 3 nested-output-model pattern (``LogoUsageRuleOutput``, "
        "``ColorEntryOutput``, ``TypographySpecOutput``, ``VoiceToneEntryOutput``, "
        "``BrandArchitectureRuleOutput``, ``PersonaProfileOutput``, "
        "``MessagingPillarOutput``, ``ElevatorPitchOutput``, ``BrandArchetypeOutput``, "
        "``DifferentiationPillarOutput``) — ``AudienceSegment`` itself must stay "
        "soft (only ``name`` required) since it also backs "
        "``StrategicCoreOutput.target_audience_segments``'s merge target. "
    )
    + _STRICT_TWIN_DOC_SUFFIX,
    name=(str, Field(min_length=1)),
    description=(str, Field(min_length=1)),
    pain_points=(List[NonEmptyStr], Field(min_length=1)),
    goals=(List[NonEmptyStr], Field(min_length=1)),
    decision_drivers=(List[NonEmptyStr], Field(min_length=1)),
)


class DifferentiationPillar(BaseModel):
    """Competitive differentiation pillar with proof points."""

    pillar: str
    proof_points: List[str] = Field(default_factory=list)
    competitive_context: str = ""


DifferentiationPillarOutput = _derive_strict_variant(
    "DifferentiationPillarOutput",
    DifferentiationPillar,
    doc=(
        "Agent-facing differentiation pillar; requires non-empty fields.\n\n"
        "Field-for-field twin of ``DifferentiationPillar`` with required content, "
        "matching the Phase 3 nested-output-model pattern (``LogoUsageRuleOutput``, "
        "``ColorEntryOutput``, ``TypographySpecOutput``, ``VoiceToneEntryOutput``, "
        "``BrandArchitectureRuleOutput``, ``PersonaProfileOutput``, "
        "``MessagingPillarOutput``, ``ElevatorPitchOutput``, ``BrandArchetypeOutput``, "
        "``AudienceSegmentOutput``) — ``DifferentiationPillar`` itself must stay "
        "soft (only ``pillar`` required) since it also backs "
        "``StrategicCoreOutput.differentiation_pillars``'s merge target. "
    )
    + _STRICT_TWIN_DOC_SUFFIX,
    pillar=(str, Field(min_length=1)),
    proof_points=(List[NonEmptyStr], Field(min_length=1)),
    competitive_context=(str, Field(min_length=1)),
)


class BrandDiscoveryAudit(BaseModel):
    """Brand discovery and audit findings.

    Fields default to empty rather than being required: this model backs
    both ``discovery_auditor``'s agent-facing ``structured_output`` schema
    and ``StrategicCoreOutput.brand_discovery``'s ``default_factory``, which
    must construct successfully with no arguments.
    """

    current_brand_perception: str = Field(
        default="",
        description="how the brand is currently perceived by its audience and market",
    )
    market_position: str = Field(
        default="", description="where the brand sits relative to competitors today"
    )
    strengths: List[str] = Field(default_factory=list, description="the brand's key strengths")
    weaknesses: List[str] = Field(default_factory=list, description="the brand's key weaknesses")
    opportunities: List[str] = Field(
        default_factory=list, description="opportunities the brand can pursue"
    )
    threats: List[str] = Field(default_factory=list, description="threats the brand faces")
    stakeholder_insights: List[str] = Field(
        default_factory=list, description="insights gathered from stakeholders"
    )


class PurposeVisionOutput(BaseModel):
    """Brand purpose, mission, and vision statements.

    Fields are required and non-empty: unlike ``StrategicCoreOutput`` (a
    merge target whose fields must default so partial per-agent fragments
    validate against it), this is the agent's *own* structured-output
    schema — an empty/omitted field here should fail Strands' validation and
    trigger a retry rather than silently accepting a blank statement.
    """

    brand_purpose: str = Field(min_length=1, description="why the company exists (one sentence)")
    mission_statement: str = Field(
        min_length=1, description="what the company does for its audience (one sentence)"
    )
    vision_statement: str = Field(
        min_length=1, description="the aspirational future state (one sentence)"
    )


class CoreValuesOutput(BaseModel):
    """A set of brand core values.

    Requires non-empty content so Strands retries blank structured_output.
    ``min_length``/``max_length`` are single-sourced from ``CORE_VALUES_MIN``/
    ``CORE_VALUES_MAX`` (the same constants the prompt interpolates).
    Uses ``CoreValueOutput`` (not the soft ``CoreValue``) so each value's
    fields are individually required — a blank value must fail validation
    instead of silently passing.
    """

    core_values: List[CoreValueOutput] = Field(
        min_length=CORE_VALUES_MIN,
        max_length=CORE_VALUES_MAX,
        description=(
            "for each value provide: value (the value name), behavioral_definition (what this "
            "value means in practice), and observable_behaviors (2-3 concrete behaviors that "
            "demonstrate this value)"
        ),
    )


class AudienceSegmentsOutput(BaseModel):
    """A set of target audience segments.

    Requires non-empty content so Strands retries blank structured_output.
    ``min_length``/``max_length`` are single-sourced from ``AUDIENCE_SEGMENTS_MIN``/
    ``AUDIENCE_SEGMENTS_MAX`` (the same constants the prompt interpolates).
    Uses ``AudienceSegmentOutput`` (not the soft ``AudienceSegment``) so each
    segment's fields are individually required — a blank-name segment must
    fail validation instead of silently passing.
    """

    target_audience_segments: List[AudienceSegmentOutput] = Field(
        min_length=AUDIENCE_SEGMENTS_MIN,
        max_length=AUDIENCE_SEGMENTS_MAX,
        description=(
            "for each segment provide: name, description, pain_points (2-3), goals (2-3), and "
            "decision_drivers (2-3)"
        ),
    )


class DifferentiationPillarsOutput(BaseModel):
    """A set of competitive differentiation pillars.

    Requires non-empty content so Strands retries blank structured_output.
    ``min_length``/``max_length`` are single-sourced from ``DIFFERENTIATION_PILLARS_MIN``/
    ``DIFFERENTIATION_PILLARS_MAX`` (the same constants the prompt interpolates).
    Uses ``DifferentiationPillarOutput`` (not the soft ``DifferentiationPillar``)
    so each pillar's fields are individually required — a blank pillar must
    fail validation instead of silently passing.
    """

    differentiation_pillars: List[DifferentiationPillarOutput] = Field(
        min_length=DIFFERENTIATION_PILLARS_MIN,
        max_length=DIFFERENTIATION_PILLARS_MAX,
        description=(
            "for each pillar provide: pillar (the differentiator name), proof_points (2-3 "
            "evidence items), and competitive_context (how competitors fall short here)"
        ),
    )


class PositioningOutput(BaseModel):
    """Synthesised positioning statement and brand promise.

    Requires non-empty content so Strands retries blank structured_output.
    """

    positioning_statement: str = Field(
        min_length=1,
        description=(
            "a single sentence following the format: 'For [audience] who need [need], "
            "[company] is the [differentiator] that delivers [value] because [proof].'"
        ),
    )
    brand_promise: str = Field(
        min_length=1, description="a one-sentence commitment to the customer"
    )


class StrategicCoreOutput(BaseModel):
    """Phase 1 output: the strategic foundation everything else derives from."""

    brand_discovery: BrandDiscoveryAudit = Field(default_factory=BrandDiscoveryAudit)
    brand_purpose: str = ""
    mission_statement: str = ""
    vision_statement: str = ""
    core_values: List[CoreValue] = Field(default_factory=list)
    brand_promise: str = ""
    positioning_statement: str = ""
    target_audience_segments: List[AudienceSegment] = Field(default_factory=list)
    differentiation_pillars: List[DifferentiationPillar] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Phase 2 — Narrative & Messaging
# ---------------------------------------------------------------------------


class BrandArchetype(BaseModel):
    """Brand archetype selection with rationale."""

    archetype: str
    rationale: str = ""
    personality_traits: List[str] = Field(default_factory=list)


BrandArchetypeOutput = _derive_strict_variant(
    "BrandArchetypeOutput",
    BrandArchetype,
    doc=(
        "Agent-facing brand archetype; requires non-empty fields.\n\n"
        "Field-for-field twin of ``BrandArchetype`` with required content, "
        "matching the Phase 3 nested-output-model pattern (``LogoUsageRuleOutput``, "
        "``ColorEntryOutput``, ``TypographySpecOutput``, ``VoiceToneEntryOutput``, "
        "``BrandArchitectureRuleOutput``, ``PersonaProfileOutput``, "
        "``MessagingPillarOutput``, ``ElevatorPitchOutput``) — ``BrandArchetype`` "
        "itself must stay soft (only ``archetype`` required) since it also backs "
        "``NarrativeMessagingOutput.brand_archetypes``'s merge target. "
    )
    + _STRICT_TWIN_DOC_SUFFIX,
    archetype=(str, Field(min_length=1)),
    rationale=(str, Field(min_length=1)),
    personality_traits=(List[NonEmptyStr], Field(min_length=1)),
)


class MessagingPillar(BaseModel):
    """A messaging pillar with proof points."""

    pillar: str
    key_message: str = ""
    proof_points: List[str] = Field(default_factory=list)


MessagingPillarOutput = _derive_strict_variant(
    "MessagingPillarOutput",
    MessagingPillar,
    doc=(
        "Agent-facing messaging pillar; requires non-empty fields.\n\n"
        "Field-for-field twin of ``MessagingPillar`` with required content, "
        "matching the Phase 3 nested-output-model pattern (``LogoUsageRuleOutput``, "
        "``ColorEntryOutput``, ``TypographySpecOutput``, ``VoiceToneEntryOutput``, "
        "``BrandArchitectureRuleOutput``, ``PersonaProfileOutput``) — "
        "``MessagingPillar`` itself must stay soft (only ``pillar`` required) since "
        "it also backs ``NarrativeMessagingOutput.messaging_framework``'s merge "
        "target. "
    )
    + _STRICT_TWIN_DOC_SUFFIX,
    pillar=(str, Field(min_length=1)),
    key_message=(str, Field(min_length=1)),
    proof_points=(List[NonEmptyStr], Field(min_length=1)),
)


class AudienceMessageMap(BaseModel):
    """Message map tailored to a specific audience segment."""

    audience_segment: str
    primary_message: str = ""
    supporting_messages: List[str] = Field(default_factory=list)
    tone_adjustments: str = ""


AudienceMessageMapOutput = _derive_strict_variant(
    "AudienceMessageMapOutput",
    AudienceMessageMap,
    doc=(
        "Agent-facing audience message map; requires non-empty fields.\n\n"
        "Field-for-field twin of ``AudienceMessageMap`` with required content, "
        "matching this file's other Phase 2/3 strict-twin pairs — "
        "``AudienceMessageMap`` itself must stay soft (only ``audience_segment`` "
        "required) since it also backs "
        "``NarrativeMessagingOutput.audience_message_maps``'s merge target. "
    )
    + _STRICT_TWIN_DOC_SUFFIX,
    audience_segment=(str, Field(min_length=1)),
    primary_message=(str, Field(min_length=1)),
    supporting_messages=(List[NonEmptyStr], Field(min_length=1)),
    tone_adjustments=(str, Field(min_length=1)),
)


class ElevatorPitch(BaseModel):
    """Tiered elevator pitch."""

    tier: str = ""  # e.g., "5-second", "30-second", "2-minute"
    pitch: str = ""


ElevatorPitchOutput = _derive_strict_variant(
    "ElevatorPitchOutput",
    ElevatorPitch,
    doc=(
        "Agent-facing elevator pitch; requires non-empty fields.\n\n"
        "Field-for-field twin of ``ElevatorPitch`` with required content, "
        "matching the Phase 3 nested-output-model pattern (``LogoUsageRuleOutput``, "
        "``ColorEntryOutput``, ``TypographySpecOutput``, ``VoiceToneEntryOutput``, "
        "``BrandArchitectureRuleOutput``, ``PersonaProfileOutput``) — "
        "``ElevatorPitch`` itself must stay soft (all-default) since it also backs "
        "``NarrativeMessagingOutput.elevator_pitches``'s merge target. "
    )
    + _STRICT_TWIN_DOC_SUFFIX,
    tier=(str, Field(min_length=1)),
    pitch=(str, Field(min_length=1)),
)


class PersonaProfile(BaseModel):
    """Rich persona profile with psychographic depth."""

    name: str
    role: str = ""
    demographics: str = ""
    psychographics: str = ""
    goals: List[str] = Field(default_factory=list)
    frustrations: List[str] = Field(default_factory=list)
    media_habits: List[str] = Field(default_factory=list)
    jobs_to_be_done: List[str] = Field(default_factory=list)


PersonaProfileOutput = _derive_strict_variant(
    "PersonaProfileOutput",
    PersonaProfile,
    doc=(
        "Agent-facing persona profile; requires non-empty fields.\n\n"
        "Field-for-field twin of ``PersonaProfile`` with required content, "
        "matching the Phase 3 nested-output-model pattern (``LogoUsageRuleOutput``, "
        "``ColorEntryOutput``, ``TypographySpecOutput``, ``VoiceToneEntryOutput``, "
        "``BrandArchitectureRuleOutput``) — ``PersonaProfile`` itself must stay soft "
        "(all-default except ``name``) since it also backs "
        "``NarrativeMessagingOutput.persona_profiles``'s merge target. "
    )
    + _STRICT_TWIN_DOC_SUFFIX,
    name=(str, Field(min_length=1)),
    role=(str, Field(min_length=1)),
    demographics=(str, Field(min_length=1)),
    psychographics=(str, Field(min_length=1)),
    goals=(List[NonEmptyStr], Field(min_length=1)),
    frustrations=(List[NonEmptyStr], Field(min_length=1)),
    media_habits=(List[NonEmptyStr], Field(min_length=1)),
    jobs_to_be_done=(List[NonEmptyStr], Field(min_length=1)),
)


class BrandStoryOutput(BaseModel):
    """Agent-facing brand story schema.

    Requires non-empty content so Strands retries blank structured_output.
    ``boilerplate_variants`` cardinality is single-sourced from
    ``BOILERPLATE_VARIANTS_COUNT`` (the same constant the prompt interpolates)
    and covers the prompt's short/medium/long versions.
    """

    brand_story: str = Field(
        min_length=1, description="a compelling 2-3 paragraph origin/purpose story"
    )
    hero_narrative: str = Field(
        min_length=1, description="a shorter, punchy version for hero sections"
    )
    boilerplate_variants: List[NonEmptyStr] = Field(
        min_length=BOILERPLATE_VARIANTS_COUNT,
        max_length=BOILERPLATE_VARIANTS_COUNT,
        description=f"{BOILERPLATE_VARIANTS_COUNT} versions (short/medium/long) for press and bios",
    )


class BrandArchetypesOutput(BaseModel):
    """Agent-facing brand archetypes schema.

    Own-field-only Phase 2 specialist model (Story 5b Step 1): contains only
    ArchetypeAnalyst's field. The chain that used to carry Storyteller's
    fields forward via subclassing is gone — Storyteller's output reaches
    ``NarrativeMessagingOutput`` exclusively through the orchestrator's flat
    union-merge of all six Phase 2 node fragments (``_merge_named_fragments``
    in ``orchestrator.py``), not through subclassing.
    Uses ``BrandArchetypeOutput`` (not the soft ``BrandArchetype``) so each
    archetype's fields are individually required — a blank archetype must
    fail validation instead of silently passing.
    """

    brand_archetypes: List[BrandArchetypeOutput] = Field(
        min_length=BRAND_ARCHETYPES_MIN, max_length=BRAND_ARCHETYPES_MAX
    )


class TaglineOutput(BaseModel):
    """Agent-facing tagline / elevator pitches schema.

    Own-field-only Phase 2 specialist model (Story 5b Step 1): contains only
    TaglineWriter's fields — see ``BrandArchetypesOutput`` for why upstream
    fields are no longer inherited.
    Uses ``ElevatorPitchOutput`` (not the soft ``ElevatorPitch``) so each of
    the ``ELEVATOR_PITCHES_COUNT`` pitch tiers is individually required — a
    blank tier or blank pitch must fail validation instead of silently passing.
    """

    tagline: str = Field(min_length=1)
    tagline_rationale: str = Field(min_length=1)
    elevator_pitches: List[ElevatorPitchOutput] = Field(
        min_length=ELEVATOR_PITCHES_COUNT, max_length=ELEVATOR_PITCHES_COUNT
    )


class MessagingFrameworkOutput(BaseModel):
    """Agent-facing messaging framework / audience maps schema.

    Own-field-only Phase 2 specialist model (Story 5b Step 1): contains only
    MessageMapper's fields — see ``BrandArchetypesOutput`` for why upstream
    fields are no longer inherited.
    Uses ``MessagingPillarOutput``/``AudienceMessageMapOutput`` (not the soft
    ``MessagingPillar``/``AudienceMessageMap``) so each nested item's fields
    are individually required — a blank pillar or audience segment must fail
    validation instead of silently producing empty output.
    """

    messaging_framework: List[MessagingPillarOutput] = Field(
        min_length=MESSAGING_PILLARS_MIN, max_length=MESSAGING_PILLARS_MAX
    )
    audience_message_maps: List[AudienceMessageMapOutput] = Field(min_length=1)


class PersonaProfilesOutput(BaseModel):
    """Agent-facing persona profiles schema.

    Own-field-only Phase 2 specialist model (Story 5b Step 1): contains only
    PersonaBuilder's field — see ``BrandArchetypesOutput`` for why upstream
    fields are no longer inherited.
    Uses ``PersonaProfileOutput`` (not the soft ``PersonaProfile``) so each
    persona's fields are individually required — a blank-name persona must
    fail validation instead of silently producing empty output.
    """

    persona_profiles: List[PersonaProfileOutput] = Field(
        min_length=PERSONA_PROFILES_MIN, max_length=PERSONA_PROFILES_MAX
    )


class WritingGuidelinesBody(BaseModel):
    """Strict writing-guidelines body nested under ``writing_guidelines``.

    Field *names* match ``WritingGuidelines`` — kept separate (not collapsed)
    because the *types* genuinely differ (``List[NonEmptyStr]`` with required
    length bounds here vs. plain ``List[str]`` with empty defaults there) and
    because collapsing would break
    ``NarrativeMessagingOutput.writing_guidelines``'s no-argument default.
    Each list's cardinality is single-sourced from its own ``*_MIN``/``*_MAX``
    constants (``VOICE_PRINCIPLES_*``, ``STYLE_DOS_*``, ``STYLE_DONTS_*``,
    ``EDITORIAL_QUALITY_BAR_*``), the same constants the prompt interpolates.
    Story 3b Step 1 finding: this pair is genuinely different, not safe to collapse.
    """

    voice_principles: List[NonEmptyStr] = Field(
        min_length=VOICE_PRINCIPLES_MIN, max_length=VOICE_PRINCIPLES_MAX
    )
    style_dos: List[NonEmptyStr] = Field(min_length=STYLE_DOS_MIN, max_length=STYLE_DOS_MAX)
    style_donts: List[NonEmptyStr] = Field(min_length=STYLE_DONTS_MIN, max_length=STYLE_DONTS_MAX)
    editorial_quality_bar: List[NonEmptyStr] = Field(
        min_length=EDITORIAL_QUALITY_BAR_MIN, max_length=EDITORIAL_QUALITY_BAR_MAX
    )


class WritingGuidelinesOutput(BaseModel):
    """Agent-facing nested writing guidelines schema.

    Own-field-only Phase 2 specialist model (Story 5b Step 1): contains only
    VoicePrinciplesDrafter's field (nested ``writing_guidelines``) — see
    ``BrandArchetypesOutput`` for why upstream fields are no longer inherited.
    VoicePrinciplesDrafter's position as last node in the linear Graph no
    longer matters for schema shape now that the orchestrator merges all six
    node fragments directly rather than only reading the last node's payload.

    Not a twin of ``WritingGuidelines`` despite the similar name — this
    class's one field is nested (``WritingGuidelinesBody``), a different
    shape and purpose than the flat merge-target ``WritingGuidelines``. Story
    3b Step 1 finding: not comparable, not safe to collapse.
    """

    writing_guidelines: WritingGuidelinesBody


class NarrativeMessagingOutput(BaseModel):
    """Phase 2 output: the verbal identity of the brand."""

    brand_story: str = ""
    hero_narrative: str = ""
    brand_archetypes: List[BrandArchetype] = Field(default_factory=list)
    tagline: str = ""
    tagline_rationale: str = ""
    messaging_framework: List[MessagingPillar] = Field(default_factory=list)
    audience_message_maps: List[AudienceMessageMap] = Field(default_factory=list)
    elevator_pitches: List[ElevatorPitch] = Field(default_factory=list)
    boilerplate_variants: List[str] = Field(default_factory=list)
    persona_profiles: List[PersonaProfile] = Field(default_factory=list)
    # Voice and writing guidelines (from VoicePrinciplesDrafter)
    writing_guidelines: "WritingGuidelines" = Field(default_factory=lambda: WritingGuidelines())


# ---------------------------------------------------------------------------
# Phase 3 — Visual & Expressive Identity
# ---------------------------------------------------------------------------


class ColorEntry(BaseModel):
    """Color palette entry with rationale."""

    name: str
    hex_value: str = ""
    usage: str = ""
    psychological_rationale: str = ""


ColorEntryOutput = _derive_strict_variant(
    "ColorEntryOutput",
    ColorEntry,
    doc=(
        "Agent-facing color entry; requires non-empty fields.\n\n"
        "Field-for-field twin of ``ColorEntry`` with required content — "
        "``ColorEntry`` itself must stay soft (only ``name`` required) since "
        "it also backs ``VisualIdentityOutput.color_palette``'s merge target. "
    )
    + _STRICT_TWIN_DOC_SUFFIX,
    name=(str, Field(min_length=1)),
    hex_value=(str, Field(min_length=1)),
    usage=(str, Field(min_length=1)),
    psychological_rationale=(str, Field(min_length=1)),
)


class TypographySpec(BaseModel):
    """Typography system specification."""

    role: str = ""  # e.g., "display", "body", "caption"
    font_family: str = ""
    weight_range: str = ""
    usage_notes: str = ""


TypographySpecOutput = _derive_strict_variant(
    "TypographySpecOutput",
    TypographySpec,
    doc=(
        "Agent-facing typography spec; requires non-empty fields.\n\n"
        "Field-for-field twin of ``TypographySpec`` with required content — "
        "``TypographySpec`` itself must stay soft (all-default) since it also "
        "backs ``VisualIdentityOutput.typography_system``'s merge target. "
    )
    + _STRICT_TWIN_DOC_SUFFIX,
    role=(str, Field(min_length=1)),
    font_family=(str, Field(min_length=1)),
    weight_range=(str, Field(min_length=1)),
    usage_notes=(str, Field(min_length=1)),
)


class LogoUsageRule(BaseModel):
    """Logo suite and usage rules."""

    variant: str = ""  # e.g., "primary", "monochrome", "icon-only"
    usage_context: str = ""
    minimum_size: str = ""
    clear_space: str = ""


LogoUsageRuleOutput = _derive_strict_variant(
    "LogoUsageRuleOutput",
    LogoUsageRule,
    doc=(
        "Agent-facing logo usage rule; requires non-empty fields.\n\n"
        "Field-for-field twin of ``LogoUsageRule`` with required content — "
        "``LogoUsageRule`` itself must stay soft (all-default) since it also "
        "backs ``VisualIdentityOutput.logo_suite``'s merge target. "
    )
    + _STRICT_TWIN_DOC_SUFFIX,
    variant=(str, Field(min_length=1)),
    usage_context=(str, Field(min_length=1)),
    minimum_size=(str, Field(min_length=1)),
    clear_space=(str, Field(min_length=1)),
)


class VoiceToneEntry(BaseModel):
    """Voice and tone spectrum entry."""

    context: str = ""  # e.g., "marketing", "support", "legal"
    tone: str = ""
    examples: List[str] = Field(default_factory=list)


VoiceToneEntryOutput = _derive_strict_variant(
    "VoiceToneEntryOutput",
    VoiceToneEntry,
    doc=(
        "Agent-facing voice/tone entry; requires non-empty fields.\n\n"
        "Field-for-field twin of ``VoiceToneEntry`` with required content — "
        "``VoiceToneEntry`` itself must stay soft (all-default) since it also "
        "backs ``VisualIdentityOutput.voice_tone_spectrum``'s merge target. "
    )
    + _STRICT_TWIN_DOC_SUFFIX,
    context=(str, Field(min_length=1)),
    tone=(str, Field(min_length=1)),
    examples=(List[str], Field(min_length=1)),
)


class VisualIdentityOutput(BaseModel):
    """Phase 3 output: the full design system and voice guide."""

    logo_suite: List[LogoUsageRule] = Field(default_factory=list)
    color_palette: List[ColorEntry] = Field(default_factory=list)
    typography_system: List[TypographySpec] = Field(default_factory=list)
    iconography_style: str = ""
    illustration_style: str = ""
    photography_direction: str = ""
    video_direction: str = ""
    motion_principles: List[str] = Field(default_factory=list)
    voice_tone_spectrum: List[VoiceToneEntry] = Field(default_factory=list)
    language_dos: List[str] = Field(default_factory=list)
    language_donts: List[str] = Field(default_factory=list)
    # Mood board candidates (from CreativeDirector collecting MoodBoardConceptualist outputs)
    mood_board_candidates: List["MoodBoardConcept"] = Field(default_factory=list)
    # Creative refinement decision (from converge_decider)
    creative_refinement: "CreativeRefinementDecision" = Field(
        default_factory=lambda: CreativeRefinementDecision()
    )
    # Design system definition (from design_system_codifier)
    design_system: "DesignSystemDefinition" = Field(
        default_factory=lambda: DesignSystemDefinition()
    )


# ---------------------------------------------------------------------------
# Phase 4 — Experience & Channel Activation
# ---------------------------------------------------------------------------


class ChannelGuideline(BaseModel):
    """Guidelines for a specific channel."""

    channel: str = ""  # e.g., "web", "social", "email", "events"
    strategy: str = ""
    dos: List[str] = Field(default_factory=list)
    donts: List[str] = Field(default_factory=list)
    content_types: List[str] = Field(default_factory=list)
    frequency_guidance: str = ""


class ChannelGuidelineOutput(BaseModel):
    """Agent-facing channel-guide schema for the six ``_make_channel_guide`` agents.

    Requires non-empty content so Strands retries blank structured_output.
    Each list's ``min_length``/``max_length`` is single-sourced from its own
    ``CHANNEL_DOS_*`` / ``CHANNEL_DONTS_*`` / ``CHANNEL_CONTENT_TYPES_*``
    constants (the same constants the prompt interpolates). Field-for-field
    twin of ``ChannelGuideline``, which itself must
    stay soft (all-default) since ``test_orchestrator.py`` and
    ``ChannelActivationOutput.channel_guidelines`` construct/merge it with
    only a subset of fields populated.
    """

    channel: str = Field(min_length=1)
    strategy: str = Field(min_length=1)
    dos: List[NonEmptyStr] = Field(min_length=CHANNEL_DOS_MIN, max_length=CHANNEL_DOS_MAX)
    donts: List[NonEmptyStr] = Field(min_length=CHANNEL_DONTS_MIN, max_length=CHANNEL_DONTS_MAX)
    content_types: List[NonEmptyStr] = Field(
        min_length=CHANNEL_CONTENT_TYPES_MIN, max_length=CHANNEL_CONTENT_TYPES_MAX
    )
    frequency_guidance: str = Field(min_length=1)


class BrandArchitectureRule(BaseModel):
    """Brand architecture rules for multi-product organizations."""

    entity: str = ""  # e.g., "parent brand", "sub-brand", "product line"
    relationship: str = ""
    naming_convention: str = ""
    visual_treatment: str = ""


BrandArchitectureRuleOutput = _derive_strict_variant(
    "BrandArchitectureRuleOutput",
    BrandArchitectureRule,
    doc=(
        "Agent-facing brand architecture rule; requires non-empty fields.\n\n"
        "Field-for-field twin of ``BrandArchitectureRule`` with required content — "
        "``BrandArchitectureRule`` itself must stay soft (all-default) since "
        "it also backs ``ChannelActivationOutput.brand_architecture``'s merge target. "
    )
    + _STRICT_TWIN_DOC_SUFFIX,
    entity=(str, Field(min_length=1)),
    relationship=(str, Field(min_length=1)),
    naming_convention=(str, Field(min_length=1)),
    visual_treatment=(str, Field(min_length=1)),
)


class BrandInActionExample(BaseModel):
    """Applied mockup or do/don't example."""

    context: str = ""
    correct_example: str = ""
    incorrect_example: str = ""
    rationale: str = ""


BrandInActionExampleOutput = _derive_strict_variant(
    "BrandInActionExampleOutput",
    BrandInActionExample,
    doc=(
        "Agent-facing brand-in-action example; requires non-empty fields.\n\n"
        "Field-for-field twin of ``BrandInActionExample`` with required content — "
        "``BrandInActionExample`` itself must stay soft (all-default) since "
        "it also backs ``ChannelActivationOutput.brand_in_action``'s merge target. "
    )
    + _STRICT_TWIN_DOC_SUFFIX,
    context=(str, Field(min_length=1)),
    correct_example=(str, Field(min_length=1)),
    incorrect_example=(str, Field(min_length=1)),
    rationale=(str, Field(min_length=1)),
)


class ChannelActivationOutput(BaseModel):
    """Phase 4 output: activation playbook for marketing execution."""

    brand_experience_principles: List[str] = Field(default_factory=list)
    signature_moments: List[str] = Field(default_factory=list)
    sensory_elements: List[str] = Field(default_factory=list)
    channel_guidelines: List[ChannelGuideline] = Field(default_factory=list)
    brand_architecture: List[BrandArchitectureRule] = Field(default_factory=list)
    naming_conventions: List[str] = Field(default_factory=list)
    terminology_glossary: Dict[str, str] = Field(default_factory=dict)
    brand_in_action: List[BrandInActionExample] = Field(default_factory=list)


class BrandExperiencePrinciplesOutput(BaseModel):
    """Agent-facing brand_experience_principler schema.

    Requires non-empty content so Strands retries blank structured_output.
    Each list's ``min_length``/``max_length`` and its ``Field(description=...)``
    (rendered directly as this agent's prompt line, since this model is used
    as ``AgentPromptSpec.structured_output``) are single-sourced from the
    same ``*_MIN``/``*_MAX`` constant pair.
    """

    brand_experience_principles: List[NonEmptyStr] = Field(
        min_length=BRAND_EXPERIENCE_PRINCIPLES_MIN,
        max_length=BRAND_EXPERIENCE_PRINCIPLES_MAX,
        description=(
            f"{BRAND_EXPERIENCE_PRINCIPLES_MIN}-{BRAND_EXPERIENCE_PRINCIPLES_MAX} principles "
            "that govern every brand touchpoint"
        ),
    )
    signature_moments: List[NonEmptyStr] = Field(
        min_length=SIGNATURE_MOMENTS_MIN,
        max_length=SIGNATURE_MOMENTS_MAX,
        description=(
            f"{SIGNATURE_MOMENTS_MIN}-{SIGNATURE_MOMENTS_MAX} key moments in the customer "
            "journey that should feel distinctly on-brand"
        ),
    )
    sensory_elements: List[NonEmptyStr] = Field(
        min_length=SENSORY_ELEMENTS_MIN,
        max_length=SENSORY_ELEMENTS_MAX,
        description=(
            f"{SENSORY_ELEMENTS_MIN}-{SENSORY_ELEMENTS_MAX} sensory cues "
            "(sound, texture, scent, etc.) if applicable"
        ),
    )


class BrandArchitectureOutput(BaseModel):
    """Agent-facing brand_architecture_builder schema.

    Requires non-empty content so Strands retries blank structured_output.
    Uses ``BrandArchitectureRuleOutput`` (not the soft ``BrandArchitectureRule``)
    so each rule's fields are individually required — a fully populated
    ``brand_architecture`` list of blank-field rules must fail validation.
    """

    brand_architecture: List[BrandArchitectureRuleOutput] = Field(
        min_length=1,
        description=(
            "rules for parent brand, sub-brands, product lines. Each with: entity, "
            "relationship, naming_convention, visual_treatment"
        ),
    )
    naming_conventions: List[NonEmptyStr] = Field(
        min_length=NAMING_CONVENTIONS_MIN,
        max_length=NAMING_CONVENTIONS_MAX,
        description=f"{NAMING_CONVENTIONS_MIN}-{NAMING_CONVENTIONS_MAX} naming rules",
    )
    terminology_glossary: Dict[NonEmptyStr, NonEmptyStr] = Field(
        min_length=TERMINOLOGY_GLOSSARY_MIN,
        max_length=TERMINOLOGY_GLOSSARY_MAX,
        description=(
            f"{TERMINOLOGY_GLOSSARY_MIN}-{TERMINOLOGY_GLOSSARY_MAX} key terms with "
            "definitions (dict)"
        ),
    )


class BrandInActionOutput(BaseModel):
    """Agent-facing brand_in_action_illustrator schema.

    Requires non-empty content so Strands retries blank structured_output.
    ``min_length``/``max_length`` are single-sourced from ``BRAND_IN_ACTION_MIN``/
    ``BRAND_IN_ACTION_MAX`` (the same constants the prompt interpolates).
    Uses ``BrandInActionExampleOutput`` (not the soft ``BrandInActionExample``)
    so each example's fields are individually required — a fully populated
    list of blank-field examples must fail validation.
    """

    brand_in_action: List[BrandInActionExampleOutput] = Field(
        min_length=BRAND_IN_ACTION_MIN,
        max_length=BRAND_IN_ACTION_MAX,
        description=(
            "each example has: context (where this applies, e.g. 'sales deck header'), "
            "correct_example (the on-brand version), incorrect_example (the off-brand "
            "version), rationale (why the correct version is better)"
        ),
    )


# ---------------------------------------------------------------------------
# Phase 5 — Governance & Evolution
# ---------------------------------------------------------------------------


class ApprovalWorkflow(BaseModel):
    """Approval workflow definition."""

    asset_type: str = ""
    approvers: List[str] = Field(default_factory=list)
    sla: str = ""
    escalation_path: str = ""


ApprovalWorkflowOutput = _derive_strict_variant(
    "ApprovalWorkflowOutput",
    ApprovalWorkflow,
    doc=(
        "Agent-facing approval workflow; requires non-empty fields.\n\n"
        "Field-for-field twin of ``ApprovalWorkflow`` with required content — "
        "``ApprovalWorkflow`` itself must stay soft (all-default) since it also "
        "backs ``GovernanceOutput.approval_workflows``'s merge target. "
    )
    + _STRICT_TWIN_DOC_SUFFIX,
    asset_type=(str, Field(min_length=1)),
    approvers=(List[NonEmptyStr], Field(min_length=1)),
    sla=(str, Field(min_length=1)),
    escalation_path=(str, Field(min_length=1)),
)


class BrandHealthKPI(BaseModel):
    """Brand health tracking metric."""

    metric: str = ""
    measurement_method: str = ""
    target: str = ""
    review_frequency: str = ""


BrandHealthKPIOutput = _derive_strict_variant(
    "BrandHealthKPIOutput",
    BrandHealthKPI,
    doc=(
        "Agent-facing brand health KPI; requires non-empty fields.\n\n"
        "Field-for-field twin of ``BrandHealthKPI`` with required content — "
        "``BrandHealthKPI`` itself must stay soft (all-default) since it also "
        "backs ``GovernanceOutput.brand_health_kpis``'s merge target. "
    )
    + _STRICT_TWIN_DOC_SUFFIX,
    metric=(str, Field(min_length=1)),
    measurement_method=(str, Field(min_length=1)),
    target=(str, Field(min_length=1)),
    review_frequency=(str, Field(min_length=1)),
)


class WikiEntry(BaseModel):
    """A single entry in the brand's living wiki/knowledge base."""

    title: str
    summary: str
    owners: List[str] = Field(default_factory=list)
    update_cadence: str = "monthly"


WikiEntryOutput = _derive_strict_variant(
    "WikiEntryOutput",
    WikiEntry,
    doc=(
        "Agent-facing wiki entry; requires non-empty fields.\n\n"
        "Field-for-field twin of ``WikiEntry`` with required content — "
        "``WikiEntry`` itself must stay soft (``title``/``summary`` unconstrained, "
        "``owners``/``update_cadence`` defaulted) since it also backs "
        "``GovernanceOutput.wiki_backlog``'s merge target. "
    )
    + _STRICT_TWIN_DOC_SUFFIX,
    title=(str, Field(min_length=1)),
    summary=(str, Field(min_length=1)),
    owners=(List[NonEmptyStr], Field(min_length=1)),
    update_cadence=(str, Field(min_length=1)),
)


class GovernanceOutput(BaseModel):
    """Phase 5 output: operational layer for sustaining the brand."""

    ownership_model: str = ""
    decision_authority: Dict[str, str] = Field(default_factory=dict)
    approval_workflows: List[ApprovalWorkflow] = Field(default_factory=list)
    agency_briefing_protocols: List[str] = Field(default_factory=list)
    asset_management_guidance: List[str] = Field(default_factory=list)
    training_onboarding_plan: List[str] = Field(default_factory=list)
    brand_health_kpis: List[BrandHealthKPI] = Field(default_factory=list)
    tracking_methodology: str = ""
    review_trigger_points: List[str] = Field(default_factory=list)
    evolution_framework: str = ""
    version_control_cadence: str = ""
    # Brand governance rules (from brand_rules_codifier)
    brand_guidelines: List[str] = Field(default_factory=list)
    # Knowledge-base backlog (from asset_wiki_planner)
    wiki_backlog: List[WikiEntry] = Field(default_factory=list)


class OwnershipOutput(BaseModel):
    """Agent-facing ownership_definer schema.

    Requires non-empty content so Strands retries blank structured_output.
    """

    ownership_model: str = Field(min_length=1, description="who owns the brand (paragraph)")
    decision_authority: Dict[NonEmptyStr, NonEmptyStr] = Field(
        min_length=1,
        description=(
            "a dict mapping decision types to responsible roles "
            "(e.g. 'logo_changes': 'Brand Director', 'campaign_messaging': 'Marketing Lead')"
        ),
    )


class ApprovalWorkflowsOutput(BaseModel):
    """Agent-facing approval_workflow_designer schema.

    Requires non-empty content so Strands retries blank structured_output.
    Each list's ``min_length``/``max_length`` is single-sourced from its own
    ``APPROVAL_WORKFLOWS_*`` / ``AGENCY_BRIEFING_PROTOCOLS_*`` constants (the
    same constants the prompt interpolates). Uses ``ApprovalWorkflowOutput``
    (not the soft ``ApprovalWorkflow``) so each workflow's fields are
    individually required.
    """

    approval_workflows: List[ApprovalWorkflowOutput] = Field(
        min_length=APPROVAL_WORKFLOWS_MIN,
        max_length=APPROVAL_WORKFLOWS_MAX,
        description=(
            f"{APPROVAL_WORKFLOWS_MIN}-{APPROVAL_WORKFLOWS_MAX} workflows, each with: "
            "asset_type, approvers (list), sla, escalation_path"
        ),
    )
    agency_briefing_protocols: List[NonEmptyStr] = Field(
        min_length=AGENCY_BRIEFING_PROTOCOLS_MIN,
        max_length=AGENCY_BRIEFING_PROTOCOLS_MAX,
        description=(
            f"{AGENCY_BRIEFING_PROTOCOLS_MIN}-{AGENCY_BRIEFING_PROTOCOLS_MAX} protocols for "
            "briefing external agencies"
        ),
    )


class AssetWikiOutput(BaseModel):
    """Agent-facing asset_wiki_planner schema.

    Requires non-empty content so Strands retries blank structured_output.
    Each list's ``min_length``/``max_length`` is single-sourced from its own
    ``ASSET_MANAGEMENT_GUIDANCE_*`` / ``WIKI_BACKLOG_*`` constants (the same
    constants the prompt interpolates). Uses ``WikiEntryOutput`` (not the soft
    ``WikiEntry``) so each entry's fields are individually required.
    """

    asset_management_guidance: List[NonEmptyStr] = Field(
        min_length=ASSET_MANAGEMENT_GUIDANCE_MIN,
        max_length=ASSET_MANAGEMENT_GUIDANCE_MAX,
        description=(
            f"{ASSET_MANAGEMENT_GUIDANCE_MIN}-{ASSET_MANAGEMENT_GUIDANCE_MAX} guidelines for "
            "managing brand assets"
        ),
    )
    wiki_backlog: List[WikiEntryOutput] = Field(
        min_length=WIKI_BACKLOG_MIN,
        max_length=WIKI_BACKLOG_MAX,
        description=(
            f"{WIKI_BACKLOG_MIN}-{WIKI_BACKLOG_MAX} wiki entries, each with: title, summary, "
            "owners (list), "
            "update_cadence. Cover: Brand North Star, Voice Playbook, Design System, Brand "
            "Review Intake, Channel Playbook, Governance Charter."
        ),
    )


class TrainingOnboardingOutput(BaseModel):
    """Agent-facing training_planner schema.

    Requires non-empty content so Strands retries blank structured_output.
    ``min_length``/``max_length`` are single-sourced from ``TRAINING_ONBOARDING_MIN``/
    ``TRAINING_ONBOARDING_MAX`` (the same constants the prompt interpolates).
    """

    training_onboarding_plan: List[NonEmptyStr] = Field(
        min_length=TRAINING_ONBOARDING_MIN,
        max_length=TRAINING_ONBOARDING_MAX,
        description=(
            f"{TRAINING_ONBOARDING_MIN}-{TRAINING_ONBOARDING_MAX} training initiatives for "
            "onboarding new team members and maintaining "
            "brand literacy."
        ),
    )


class BrandHealthKPIsOutput(BaseModel):
    """Agent-facing kpi_designer schema.

    Requires non-empty content so Strands retries blank structured_output.
    Each list's ``min_length``/``max_length`` is single-sourced from its own
    ``BRAND_HEALTH_KPIS_*`` / ``REVIEW_TRIGGER_POINTS_*`` constants (the same
    constants the prompt interpolates). Uses ``BrandHealthKPIOutput`` (not the
    soft ``BrandHealthKPI``) so each KPI's fields are individually required.
    """

    brand_health_kpis: List[BrandHealthKPIOutput] = Field(
        min_length=BRAND_HEALTH_KPIS_MIN,
        max_length=BRAND_HEALTH_KPIS_MAX,
        description=(
            f"{BRAND_HEALTH_KPIS_MIN}-{BRAND_HEALTH_KPIS_MAX} KPIs, each with: metric, "
            "measurement_method, target, review_frequency"
        ),
    )
    tracking_methodology: str = Field(
        min_length=1, description="paragraph describing the measurement approach"
    )
    review_trigger_points: List[NonEmptyStr] = Field(
        min_length=REVIEW_TRIGGER_POINTS_MIN,
        max_length=REVIEW_TRIGGER_POINTS_MAX,
        description=(
            f"{REVIEW_TRIGGER_POINTS_MIN}-{REVIEW_TRIGGER_POINTS_MAX} events that should "
            "trigger a brand health review"
        ),
    )


class EvolutionFrameworkOutput(BaseModel):
    """Agent-facing evolution_framer schema.

    Requires non-empty content so Strands retries blank structured_output.
    """

    evolution_framework: str = Field(
        min_length=1, description="paragraph describing how the brand evolves over time"
    )
    version_control_cadence: str = Field(
        min_length=1,
        description="how often the brand system is formally reviewed and versioned",
    )


class BrandGuidelinesOutput(BaseModel):
    """Agent-facing brand_rules_codifier schema.

    Requires non-empty content so Strands retries blank structured_output.
    ``min_length``/``max_length`` are single-sourced from ``BRAND_GUIDELINES_MIN``/
    ``BRAND_GUIDELINES_MAX`` (the same constants the prompt interpolates).
    """

    brand_guidelines: List[NonEmptyStr] = Field(
        min_length=BRAND_GUIDELINES_MIN,
        max_length=BRAND_GUIDELINES_MAX,
        description=(
            f"a list of {BRAND_GUIDELINES_MIN}-{BRAND_GUIDELINES_MAX} governance rules that "
            "everyone in the organisation must follow. "
            "Each rule is a single clear sentence."
        ),
    )


# ---------------------------------------------------------------------------
# Phase gate tracking
# ---------------------------------------------------------------------------


class PhaseGate(BaseModel):
    """Tracks the approval state of a phase transition."""

    phase: BrandPhase
    status: PhaseGateStatus = PhaseGateStatus.NOT_STARTED
    reviewer_feedback: str = ""


# ---------------------------------------------------------------------------
# Composite output
# ---------------------------------------------------------------------------


class BrandCheckRequest(BaseModel):
    """Request to evaluate a marketing asset against a brand's guidelines."""

    asset_name: str
    asset_description: str


class BrandCheckResult(BaseModel):
    """Verdict from :class:`BrandComplianceAgent` on-brand evaluation of a single asset."""

    asset_name: str
    is_on_brand: bool
    confidence: float = Field(ge=0, le=1)
    rationale: List[str] = Field(default_factory=list)
    revision_suggestions: List[str] = Field(default_factory=list)


class CompetitiveSnapshot(BaseModel):
    """Market research result: competitive and similar brands context."""

    summary: str = ""
    similar_brands: List[str] = Field(default_factory=list)
    insights: List[str] = Field(default_factory=list)
    source: str = "market_research_team"


class DesignAssetRequestResult(BaseModel):
    """Result of a design asset request (stub or from a configured design service)."""

    request_id: str
    status: str = "pending"
    artifacts: List[str] = Field(default_factory=list)


class BrandBook(BaseModel):
    """Consolidated brand document for handoff."""

    content: str = ""
    sections: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Shared models (used as sub-models inside phase outputs)
# ---------------------------------------------------------------------------


class MoodBoardConcept(BaseModel):
    """A single mood-board direction.

    Collapsed twin (Story 3b Step 2): used directly both as
    ``MoodBoardConceptualist_*``'s ``structured_output=`` (agents.py) and as
    the nested item type for ``MoodBoardCandidatesOutput.mood_board_candidates``
    and ``VisualIdentityOutput.mood_board_candidates``, following the same
    single-model pattern as ``BrandDiscoveryAudit`` — see the module
    docstring. The former strict ``MoodBoardConceptOutput`` twin (Step 1
    finding: field/type identical, differing only in default/required
    strictness) has been removed.
    """

    title: str = Field(description="a name for this direction")
    visual_direction: str = Field(description="overall aesthetic description")
    color_story: List[str] = Field(default_factory=list, description="3-4 color names/descriptions")
    typography_direction: str = Field(description="font style recommendations")
    image_style: List[str] = Field(default_factory=list, description="3-4 image style descriptions")


class CreativeRefinementDecision(BaseModel):
    """Phase 3 converge node output: which moodboard direction won and why.

    Collapsed twin (Story 3b Step 2): used directly both as
    ``converge_decider``'s ``structured_output=`` (agents.py) and as
    ``VisualIdentityOutput.creative_refinement``'s ``default_factory`` merge
    target, following the same single-model pattern as ``BrandDiscoveryAudit``
    — see the module docstring. The former strict
    ``CreativeRefinementDecisionOutput`` twin (Step 1 finding: field/type
    identical, differing only in default/required strictness) has been
    removed.
    """

    winning_candidate_title: str = Field(default="", description="the selected candidate title")
    scoring_criteria: List[str] = Field(
        default_factory=list, description="the criteria used to score candidates"
    )
    scores_by_candidate: Dict[str, float] = Field(
        default_factory=dict, description="dict of title→score"
    )
    rationale: str = Field(default="", description="why this candidate won")
    workshop_prompts: List[str] = Field(
        default_factory=list, description="3 questions for stakeholders"
    )
    decision_criteria: List[str] = Field(default_factory=list, description="decision criteria used")


class WritingGuidelines(BaseModel):
    """Voice/tone and editorial rules; merge target nested at
    ``NarrativeMessagingOutput.writing_guidelines``.

    Its real structural counterpart is ``WritingGuidelinesBody`` (not
    ``WritingGuidelinesOutput``, which is a different, much larger construct —
    see that class's docstring). Field *names* match ``WritingGuidelinesBody``,
    but the *types* don't: this side is ``List[str]`` with no cardinality bound
    and empty defaults, while ``WritingGuidelinesBody`` is ``List[NonEmptyStr]``
    with a required ``min_length=3, max_length=4``. Genuinely different — not
    safe to collapse (Story 3b Step 1 finding).
    """

    voice_principles: List[str] = Field(default_factory=list)
    style_dos: List[str] = Field(default_factory=list)
    style_donts: List[str] = Field(default_factory=list)
    editorial_quality_bar: List[str] = Field(default_factory=list)


class DesignSystemDefinition(BaseModel):
    """Codified design system.

    Collapsed twin (Story 3b Step 2): used directly both as
    ``design_system_codifier``'s ``structured_output=`` (agents.py) and as
    ``VisualIdentityOutput.design_system``'s ``default_factory`` merge
    target, following the same single-model pattern as ``BrandDiscoveryAudit``
    — see the module docstring. The former strict
    ``DesignSystemDefinitionOutput`` twin (Step 1 finding: field/type
    identical, differing only in default/required strictness) has been
    removed.
    """

    design_principles: List[str] = Field(
        default_factory=list,
        description="3-4 guiding principles (e.g. 'Clarity over decoration')",
    )
    foundation_tokens: List[str] = Field(
        default_factory=list,
        description="4-6 token categories (color, type, spacing, motion, etc.)",
    )
    component_standards: List[str] = Field(
        default_factory=list,
        description="3-5 component rules (buttons, cards, navigation, etc.)",
    )


# ---------------------------------------------------------------------------
# Phase 3 agent-facing structured_output schemas
# ---------------------------------------------------------------------------
# Merge targets above keep empty defaults so partial fragments validate.
# Agent schemas below require content so Strands retries blank output.


class MoodBoardCandidatesOutput(BaseModel):
    """Agent-facing CreativeDirector schema: collected moodboard candidates.

    ``min_length``/``max_length`` encode the diverge fan-out of 2–3 concepts.
    Nested entries use ``MoodBoardConcept`` (the collapsed twin — see that
    class's docstring), so this list's own cardinality bound is what guards
    against a blank collection; individual concepts keep their defaults.
    """

    mood_board_candidates: List[MoodBoardConcept] = Field(
        min_length=2,
        max_length=3,
        description=(
            "preserve each concept (title, visual_direction, color_story, "
            "typography_direction, image_style)"
        ),
    )


class LogoSuiteOutput(BaseModel):
    """Agent-facing logo_specifier schema.

    ``min_length``/``max_length`` encode the prompt's four logo variants.
    """

    logo_suite: List[LogoUsageRuleOutput] = Field(
        min_length=4,
        max_length=4,
        description="variant, usage_context, minimum_size, clear_space",
    )


class ColorPaletteSystemOutput(BaseModel):
    """Agent-facing color_system_builder schema.

    Named to avoid colliding with mission ``ColorPalette``.
    ``min_length``/``max_length`` are single-sourced from ``COLOR_PALETTE_MIN``/
    ``COLOR_PALETTE_MAX`` (the same constants the prompt interpolates).
    """

    color_palette: List[ColorEntryOutput] = Field(
        min_length=COLOR_PALETTE_MIN,
        max_length=COLOR_PALETTE_MAX,
        description=(
            "for each: name, hex_value, usage (where to use it), and "
            "psychological_rationale (why this color works for the brand)"
        ),
    )


class TypographySystemOutput(BaseModel):
    """Agent-facing typography_builder schema.

    ``min_length``/``max_length`` are single-sourced from ``TYPOGRAPHY_SYSTEM_MIN``/
    ``TYPOGRAPHY_SYSTEM_MAX`` (the same constants the prompt interpolates).
    """

    typography_system: List[TypographySpecOutput] = Field(
        min_length=TYPOGRAPHY_SYSTEM_MIN,
        max_length=TYPOGRAPHY_SYSTEM_MAX,
        description="role, font_family, weight_range, usage_notes",
    )


class IconographyOutput(BaseModel):
    """Agent-facing iconography_director schema."""

    iconography_style: str = Field(
        min_length=1,
        description="describe the icon aesthetic (line weight, corner radius, fill)",
    )
    illustration_style: str = Field(
        min_length=1,
        description="describe the illustration approach (flat, isometric, etc.)",
    )


class PhotographyVideoOutput(BaseModel):
    """Agent-facing photography_video_director schema.

    ``motion_principles`` cardinality is single-sourced from ``MOTION_PRINCIPLES_MIN``/
    ``MOTION_PRINCIPLES_MAX`` (the same constants the prompt interpolates).
    """

    photography_direction: str = Field(
        min_length=1, description="shooting style, lighting, composition, subjects"
    )
    video_direction: str = Field(
        min_length=1, description="pacing, tone, visual style for video content"
    )
    motion_principles: List[str] = Field(
        min_length=MOTION_PRINCIPLES_MIN,
        max_length=MOTION_PRINCIPLES_MAX,
        description=(
            f"{MOTION_PRINCIPLES_MIN}-{MOTION_PRINCIPLES_MAX} principles for "
            "animation/motion design"
        ),
    )


class VoiceToneOutput(BaseModel):
    """Agent-facing voice_tone_builder schema.

    ``language_dos``/``language_donts`` cardinalities are single-sourced from
    ``LANGUAGE_DOS_*`` / ``LANGUAGE_DONTS_*`` (the same constants the prompt
    interpolates).

    ``voice_tone_spectrum``'s "2-3 examples" phrase is prompt guidance only,
    not a single-sourced constraint: unlike the fields above, no
    ``Field(min_length=2, max_length=3)`` backs it — the per-entry
    ``VoiceToneEntryOutput.examples`` list only requires ``min_length=1``
    (non-blank). There is no drift to fix; this is an intentional exception
    to the single-sourcing pattern documented at the top of this file.
    """

    voice_tone_spectrum: List[VoiceToneEntryOutput] = Field(
        min_length=1,
        description=(
            "for each context (marketing, support, legal, social, internal), specify the "
            "tone and 2-3 examples"
        ),
    )
    language_dos: List[str] = Field(
        min_length=LANGUAGE_DOS_MIN,
        max_length=LANGUAGE_DOS_MAX,
        description=f"{LANGUAGE_DOS_MIN}-{LANGUAGE_DOS_MAX} approved language patterns",
    )
    language_donts: List[str] = Field(
        min_length=LANGUAGE_DONTS_MIN,
        max_length=LANGUAGE_DONTS_MAX,
        description=f"{LANGUAGE_DONTS_MIN}-{LANGUAGE_DONTS_MAX} language anti-patterns",
    )


# ---------------------------------------------------------------------------
# Team output — all phase outputs plus cross-cutting results
# ---------------------------------------------------------------------------


class TeamOutput(BaseModel):
    """Aggregate output of a branding run: status, phase artifacts, and cross-cutting results.

    Invariants:
        - ``degraded_phases`` lists every reached ``BrandPhase`` whose structured
          output could not be parsed from the LLM's response and was defaulted to
          a bare ``model_class()`` instance instead. ``orchestrator._extract_phase_output``
          is the sole source of the per-phase ``degraded`` flag; ``orchestrator.run``
          (thread path) and the Temporal finalize activity both fold those flags
          into this list via ``_assemble_team_output``'s ``degraded_phases`` parameter.
          A phase absent from this list — including one not reached this run —
          carries no claim either way; only membership is meaningful. An empty
          list means every reached phase's output was a successfully parsed LLM
          response.
    """

    status: WorkflowStatus
    mission_summary: str
    current_phase: BrandPhase = BrandPhase.STRATEGIC_CORE
    phase_gates: List[PhaseGate] = Field(default_factory=list)
    degraded_phases: List[BrandPhase] = Field(default_factory=list)

    # Phase outputs
    strategic_core: Optional[StrategicCoreOutput] = None
    narrative_messaging: Optional[NarrativeMessagingOutput] = None
    visual_identity: Optional[VisualIdentityOutput] = None
    channel_activation: Optional[ChannelActivationOutput] = None
    governance: Optional[GovernanceOutput] = None

    # Non-phase outputs
    brand_checks: List[BrandCheckResult] = Field(default_factory=list)
    human_feedback: Optional[str] = None
    competitive_snapshot: Optional[CompetitiveSnapshot] = None
    design_asset_result: Optional[DesignAssetRequestResult] = None
    brand_book: Optional[BrandBook] = None


# ---------------------------------------------------------------------------
# Brand version + top-level Brand model
# ---------------------------------------------------------------------------


class BrandVersionSummary(BaseModel):
    """Summary of a single brand run version for history."""

    version: int
    created_at: str
    status: Optional[str] = None


class BrandConsumerContext(BaseModel):
    """Flattened, cross-team consumer view of a brand's Phase 1/2 outputs.

    A stable, in-process shape other teams can consume without re-deriving the
    extraction against the nested ``StrategicCoreOutput`` /
    ``NarrativeMessagingOutput`` phase schemas they do not own. Produced by
    :meth:`Brand.to_consumer_context`. Field names deliberately mirror the
    social-marketing ``BrandContext`` adapter shape so a consumer can build one
    from this via ``model_dump()`` without a remap.

    Every field carries a default so an under-populated or degraded brand (no
    ``latest_output``, or missing Phase 1/2 outputs) still yields a fully
    constructed context. ``voice_and_tone`` defaults to the same fallback the
    accessor applies when its sources are empty (``"professional, clear, and
    human"``). ``brand_name`` defaults to the generic placeholder ``"Brand"``
    for direct construction; the accessor itself does not use that placeholder —
    it falls back to ``mission.company_name`` when a ``Brand`` has no name.
    """

    brand_name: str = "Brand"
    target_audience: str = ""
    voice_and_tone: str = "professional, clear, and human"
    brand_guidelines: str = ""
    brand_objectives: str = ""
    messaging_pillars: List[str] = Field(default_factory=list)
    brand_story: str = ""
    tagline: str = ""


class Brand(BaseModel):
    """A brand owned by a client; can be evolved over time."""

    id: str
    client_id: str
    name: str = Field(..., min_length=1)
    status: BrandStatus = BrandStatus.draft
    current_phase: BrandPhase = BrandPhase.STRATEGIC_CORE
    mission: BrandingMission
    latest_output: Optional[TeamOutput] = None
    conversation_id: Optional[str] = None
    version: int = 0
    history: List[BrandVersionSummary] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def to_consumer_context(self) -> BrandConsumerContext:
        """Flatten this brand's Phase 1/2 outputs into a consumer-facing context.

        Synthesizes audience, voice, guidelines, objectives, and messaging from
        ``self.name``, ``self.mission``, and — when present — the strategic-core
        (Phase 1) and narrative-messaging (Phase 2) outputs on
        ``self.latest_output``. Mirrors the extraction the social-marketing
        branding adapter hand-parses from the raw brand JSON, but operates on the
        typed in-process models so any team holding a ``Brand`` can reuse it.

        Preconditions:
            - ``self`` is a valid ``Brand`` (in particular ``self.name`` is a
              non-empty string and ``self.mission`` is a ``BrandingMission``).
        Postconditions:
            - Returns a ``BrandConsumerContext``; never raises for a missing or
              ``None`` ``latest_output`` / ``strategic_core`` / ``narrative_messaging``.
            - ``brand_name`` is ``self.name`` (falling back to
              ``self.mission.company_name`` only if ``self.name`` is falsy).
            - Phase-derived fields (``target_audience`` beyond the mission
              audience, ``brand_guidelines``, ``brand_objectives``,
              ``messaging_pillars``, ``brand_story``, ``tagline``) are empty when
              their source phase output is absent.
            - ``voice_and_tone`` is the mission voice plus any archetype traits,
              falling back to ``"professional, clear, and human"`` only when both
              are empty.
        """
        mission = self.mission
        output = self.latest_output
        strategic: Optional[StrategicCoreOutput] = output.strategic_core if output else None
        narrative: Optional[NarrativeMessagingOutput] = (
            output.narrative_messaging if output else None
        )

        brand_name = self.name or mission.company_name

        # Target audience -- mission audience plus per-segment detail.
        audience_parts = [mission.target_audience]
        if strategic:
            for seg in strategic.target_audience_segments:
                if seg.name:
                    audience_parts.append(
                        f"{seg.name}: {seg.description}" if seg.description else seg.name
                    )
        target_audience = "; ".join(p for p in audience_parts if p)

        # Voice and tone -- mission voice plus archetype personality traits.
        voice_parts = [mission.desired_voice]
        if narrative:
            for archetype in narrative.brand_archetypes:
                if archetype.personality_traits:
                    voice_parts.append(", ".join(archetype.personality_traits[:5]))
        voice_and_tone = "; ".join(p for p in voice_parts if p) or "professional, clear, and human"

        # Brand guidelines -- synthesized from strategic core + narrative.
        guideline_parts: List[str] = []
        if strategic:
            if strategic.positioning_statement:
                guideline_parts.append(f"Positioning: {strategic.positioning_statement}")
            for val in strategic.core_values:
                if val.value:
                    guideline_parts.append(f"Value -- {val.value}: {val.behavioral_definition}")
            for pillar in strategic.differentiation_pillars:
                if pillar.pillar:
                    guideline_parts.append(
                        f"Differentiator -- {pillar.pillar}: {pillar.competitive_context}"
                    )
        if narrative:
            for aud_map in narrative.audience_message_maps:
                if aud_map.tone_adjustments:
                    guideline_parts.append(
                        f"Tone for {aud_map.audience_segment or 'audience'}: "
                        f"{aud_map.tone_adjustments}"
                    )
        brand_guidelines = "\n".join(guideline_parts)

        # Brand objectives -- purpose/mission/vision/promise from strategic core.
        objective_parts: List[str] = []
        if strategic:
            if strategic.brand_purpose:
                objective_parts.append(f"Purpose: {strategic.brand_purpose}")
            if strategic.mission_statement:
                objective_parts.append(f"Mission: {strategic.mission_statement}")
            if strategic.vision_statement:
                objective_parts.append(f"Vision: {strategic.vision_statement}")
            if strategic.brand_promise:
                objective_parts.append(f"Promise: {strategic.brand_promise}")
        brand_objectives = "\n".join(objective_parts)

        # Messaging pillars -- pillar names from the narrative framework.
        messaging_pillars: List[str] = []
        if narrative:
            messaging_pillars = [mp.pillar for mp in narrative.messaging_framework if mp.pillar]

        return BrandConsumerContext(
            brand_name=brand_name,
            target_audience=target_audience,
            voice_and_tone=voice_and_tone,
            brand_guidelines=brand_guidelines,
            brand_objectives=brand_objectives,
            messaging_pillars=messaging_pillars,
            brand_story=narrative.brand_story if narrative else "",
            tagline=narrative.tagline if narrative else "",
        )
