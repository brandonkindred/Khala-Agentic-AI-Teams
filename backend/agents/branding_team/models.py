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

This pattern does not apply to the Phase 2 cumulative-inheritance chain
(``BrandStoryOutput`` → … → ``WritingGuidelinesOutput``) or to remaining
hand-written sibling pairs that were never collapsed
(``BrandDiscoveryAudit`` / ``BrandDiscoveryAuditOutput``,
``ChannelGuideline`` / ``ChannelGuidelineOutput``, and the mood-board /
design-system agent-output wrappers).
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

    Fields default to empty rather than being required: this model also
    backs ``StrategicCoreOutput.brand_discovery``'s ``default_factory``, which
    must construct successfully with no arguments. ``discovery_auditor``'s own
    agent-facing schema is the stricter ``BrandDiscoveryAuditOutput`` below.
    """

    current_brand_perception: str = ""
    market_position: str = ""
    strengths: List[str] = Field(default_factory=list)
    weaknesses: List[str] = Field(default_factory=list)
    opportunities: List[str] = Field(default_factory=list)
    threats: List[str] = Field(default_factory=list)
    stakeholder_insights: List[str] = Field(default_factory=list)


class BrandDiscoveryAuditOutput(BaseModel):
    """Agent-facing brand discovery schema.

    Requires non-empty content so Strands retries blank structured_output.
    Field-for-field identical to ``BrandDiscoveryAudit`` — kept as a separate
    model so this one can require real content without breaking
    ``StrategicCoreOutput.brand_discovery``'s no-argument default construction.
    """

    current_brand_perception: str = Field(min_length=1)
    market_position: str = Field(min_length=1)
    strengths: List[NonEmptyStr] = Field(min_length=1)
    weaknesses: List[NonEmptyStr] = Field(min_length=1)
    opportunities: List[NonEmptyStr] = Field(min_length=1)
    threats: List[NonEmptyStr] = Field(min_length=1)
    stakeholder_insights: List[NonEmptyStr] = Field(min_length=1)


class PurposeVisionOutput(BaseModel):
    """Brand purpose, mission, and vision statements.

    Fields are required and non-empty: unlike ``StrategicCoreOutput`` (a
    merge target whose fields must default so partial per-agent fragments
    validate against it), this is the agent's *own* structured-output
    schema — an empty/omitted field here should fail Strands' validation and
    trigger a retry rather than silently accepting a blank statement.
    """

    brand_purpose: str = Field(min_length=1)
    mission_statement: str = Field(min_length=1)
    vision_statement: str = Field(min_length=1)


class CoreValuesOutput(BaseModel):
    """A set of brand core values.

    Requires non-empty content so Strands retries blank structured_output.
    ``min_length``/``max_length`` encode the prompt's stated "3-5 core values".
    Uses ``CoreValueOutput`` (not the soft ``CoreValue``) so each value's
    fields are individually required — a blank value must fail validation
    instead of silently passing.
    """

    core_values: List[CoreValueOutput] = Field(
        min_length=CORE_VALUES_MIN, max_length=CORE_VALUES_MAX
    )


class AudienceSegmentsOutput(BaseModel):
    """A set of target audience segments.

    Requires non-empty content so Strands retries blank structured_output.
    ``min_length``/``max_length`` encode the prompt's stated "1-3 target audience segments".
    Uses ``AudienceSegmentOutput`` (not the soft ``AudienceSegment``) so each
    segment's fields are individually required — a blank-name segment must
    fail validation instead of silently passing.
    """

    target_audience_segments: List[AudienceSegmentOutput] = Field(
        min_length=AUDIENCE_SEGMENTS_MIN, max_length=AUDIENCE_SEGMENTS_MAX
    )


class DifferentiationPillarsOutput(BaseModel):
    """A set of competitive differentiation pillars.

    Requires non-empty content so Strands retries blank structured_output.
    ``min_length``/``max_length`` encode the prompt's stated "2-4 differentiation pillars".
    Uses ``DifferentiationPillarOutput`` (not the soft ``DifferentiationPillar``)
    so each pillar's fields are individually required — a blank pillar must
    fail validation instead of silently passing.
    """

    differentiation_pillars: List[DifferentiationPillarOutput] = Field(
        min_length=DIFFERENTIATION_PILLARS_MIN, max_length=DIFFERENTIATION_PILLARS_MAX
    )


class PositioningOutput(BaseModel):
    """Synthesised positioning statement and brand promise.

    Requires non-empty content so Strands retries blank structured_output.
    """

    positioning_statement: str = Field(min_length=1)
    brand_promise: str = Field(min_length=1)


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
    ``boilerplate_variants`` cardinality encodes the prompt's stated "3 versions
    (short/medium/long)".
    """

    brand_story: str = Field(min_length=1)
    hero_narrative: str = Field(min_length=1)
    boilerplate_variants: List[NonEmptyStr] = Field(
        min_length=BOILERPLATE_VARIANTS_COUNT, max_length=BOILERPLATE_VARIANTS_COUNT
    )


class BrandArchetypesOutput(BrandStoryOutput):
    """Story carry-forward plus brand archetypes.

    Inherits the Storyteller fields so a linear Graph predecessor's
    ``structured_output`` already exposes the brand story to TaglineWriter
    (Strands Graph node inputs only include direct dependency results, and
    multi-in edges use OR-ready semantics so cumulative fan-in is unsafe).
    Uses ``BrandArchetypeOutput`` (not the soft ``BrandArchetype``) so each
    archetype's fields are individually required — a blank archetype must
    fail validation instead of silently passing.
    """

    brand_archetypes: List[BrandArchetypeOutput] = Field(
        min_length=BRAND_ARCHETYPES_MIN, max_length=BRAND_ARCHETYPES_MAX
    )


class TaglineOutput(BrandArchetypesOutput):
    """Prior narrative carry-forward plus tagline / elevator pitches.

    Uses ``ElevatorPitchOutput`` (not the soft ``ElevatorPitch``) so each
    pitch's fields are individually required — three blank-tier/blank-pitch
    entries must fail validation instead of silently passing.
    """

    tagline: str = Field(min_length=1)
    tagline_rationale: str = Field(min_length=1)
    elevator_pitches: List[ElevatorPitchOutput] = Field(
        min_length=ELEVATOR_PITCHES_COUNT, max_length=ELEVATOR_PITCHES_COUNT
    )


class MessagingFrameworkOutput(TaglineOutput):
    """Prior narrative carry-forward plus messaging framework / audience maps.

    Uses ``MessagingPillarOutput``/``AudienceMessageMapOutput`` (not the soft
    ``MessagingPillar``/``AudienceMessageMap``) so each nested item's fields
    are individually required — a blank pillar or audience segment must fail
    validation instead of silently producing empty output.
    """

    messaging_framework: List[MessagingPillarOutput] = Field(
        min_length=MESSAGING_PILLARS_MIN, max_length=MESSAGING_PILLARS_MAX
    )
    audience_message_maps: List[AudienceMessageMapOutput] = Field(min_length=1)


class PersonaProfilesOutput(MessagingFrameworkOutput):
    """Prior narrative carry-forward plus persona profiles.

    Uses ``PersonaProfileOutput`` (not the soft ``PersonaProfile``) so each
    persona's fields are individually required — a blank-name persona must
    fail validation instead of silently producing empty output.
    """

    persona_profiles: List[PersonaProfileOutput] = Field(
        min_length=PERSONA_PROFILES_MIN, max_length=PERSONA_PROFILES_MAX
    )


class WritingGuidelinesBody(BaseModel):
    """Strict writing-guidelines body nested under ``writing_guidelines``.

    Field-for-field identical to ``WritingGuidelines`` — kept separate so this
    one can require real content without breaking
    ``NarrativeMessagingOutput.writing_guidelines``'s no-argument default.
    Cardinalities encode the prompt's stated "3-4" for each list.
    """

    voice_principles: List[NonEmptyStr] = Field(
        min_length=VOICE_PRINCIPLES_MIN, max_length=VOICE_PRINCIPLES_MAX
    )
    style_dos: List[NonEmptyStr] = Field(min_length=STYLE_DOS_MIN, max_length=STYLE_DOS_MAX)
    style_donts: List[NonEmptyStr] = Field(min_length=STYLE_DONTS_MIN, max_length=STYLE_DONTS_MAX)
    editorial_quality_bar: List[NonEmptyStr] = Field(
        min_length=EDITORIAL_QUALITY_BAR_MIN, max_length=EDITORIAL_QUALITY_BAR_MAX
    )


class WritingGuidelinesOutput(PersonaProfilesOutput):
    """Full Phase 2 carry-forward plus nested writing guidelines.

    VoicePrinciplesDrafter is last in the linear Graph, so its payload must
    include every upstream fragment plus ``writing_guidelines`` in the shape
    ``NarrativeMessagingOutput`` expects (no nest-under remap needed).
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
    data_visualization_style: str = ""
    digital_adaptations: List[str] = Field(default_factory=list)
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
    ``min_length``/``max_length`` encode the prompt's stated cardinalities
    ("3-4 best practices" / "3-4 things to avoid" / "3-5 recommended content
    formats"). Field-for-field twin of ``ChannelGuideline``, which itself must
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
    """Agent-facing brand_experience_principles schema.

    Requires non-empty content so Strands retries blank structured_output.
    ``min_length``/``max_length`` encode the prompt's stated cardinalities.
    """

    brand_experience_principles: List[NonEmptyStr] = Field(
        min_length=BRAND_EXPERIENCE_PRINCIPLES_MIN, max_length=BRAND_EXPERIENCE_PRINCIPLES_MAX
    )
    signature_moments: List[NonEmptyStr] = Field(
        min_length=SIGNATURE_MOMENTS_MIN, max_length=SIGNATURE_MOMENTS_MAX
    )
    sensory_elements: List[NonEmptyStr] = Field(
        min_length=SENSORY_ELEMENTS_MIN, max_length=SENSORY_ELEMENTS_MAX
    )


class BrandArchitectureOutput(BaseModel):
    """Agent-facing brand_architecture_builder schema.

    Requires non-empty content so Strands retries blank structured_output.
    Uses ``BrandArchitectureRuleOutput`` (not the soft ``BrandArchitectureRule``)
    so each rule's fields are individually required — a fully populated
    ``brand_architecture`` list of blank-field rules must fail validation.
    """

    brand_architecture: List[BrandArchitectureRuleOutput] = Field(min_length=1)
    naming_conventions: List[NonEmptyStr] = Field(
        min_length=NAMING_CONVENTIONS_MIN, max_length=NAMING_CONVENTIONS_MAX
    )
    terminology_glossary: Dict[NonEmptyStr, NonEmptyStr] = Field(
        min_length=TERMINOLOGY_GLOSSARY_MIN, max_length=TERMINOLOGY_GLOSSARY_MAX
    )


class BrandInActionOutput(BaseModel):
    """Agent-facing brand_in_action_illustrator schema.

    Requires non-empty content so Strands retries blank structured_output.
    ``min_length``/``max_length`` encode the prompt's stated "3-5 applied examples".
    Uses ``BrandInActionExampleOutput`` (not the soft ``BrandInActionExample``)
    so each example's fields are individually required — a fully populated
    list of blank-field examples must fail validation.
    """

    brand_in_action: List[BrandInActionExampleOutput] = Field(
        min_length=BRAND_IN_ACTION_MIN, max_length=BRAND_IN_ACTION_MAX
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

    ownership_model: str = Field(min_length=1)
    decision_authority: Dict[NonEmptyStr, NonEmptyStr] = Field(min_length=1)


class ApprovalWorkflowsOutput(BaseModel):
    """Agent-facing approval_workflow_designer schema.

    Requires non-empty content so Strands retries blank structured_output.
    ``min_length``/``max_length`` encode the prompt's stated "3-5 workflows"
    / "3-5 protocols". Uses ``ApprovalWorkflowOutput`` (not the soft
    ``ApprovalWorkflow``) so each workflow's fields are individually
    required.
    """

    approval_workflows: List[ApprovalWorkflowOutput] = Field(
        min_length=APPROVAL_WORKFLOWS_MIN, max_length=APPROVAL_WORKFLOWS_MAX
    )
    agency_briefing_protocols: List[NonEmptyStr] = Field(
        min_length=AGENCY_BRIEFING_PROTOCOLS_MIN, max_length=AGENCY_BRIEFING_PROTOCOLS_MAX
    )


class AssetWikiOutput(BaseModel):
    """Agent-facing asset_wiki_planner schema.

    Requires non-empty content so Strands retries blank structured_output.
    ``min_length``/``max_length`` encode the prompt's stated "3-5 guidelines"
    / "4-6 wiki entries". Uses ``WikiEntryOutput`` (not the soft
    ``WikiEntry``) so each entry's fields are individually required.
    """

    asset_management_guidance: List[NonEmptyStr] = Field(
        min_length=ASSET_MANAGEMENT_GUIDANCE_MIN, max_length=ASSET_MANAGEMENT_GUIDANCE_MAX
    )
    wiki_backlog: List[WikiEntryOutput] = Field(
        min_length=WIKI_BACKLOG_MIN, max_length=WIKI_BACKLOG_MAX
    )


class TrainingOnboardingOutput(BaseModel):
    """Agent-facing training_planner schema.

    Requires non-empty content so Strands retries blank structured_output.
    ``min_length``/``max_length`` encode the prompt's stated "4-6 training
    initiatives".
    """

    training_onboarding_plan: List[NonEmptyStr] = Field(
        min_length=TRAINING_ONBOARDING_MIN, max_length=TRAINING_ONBOARDING_MAX
    )


class BrandHealthKPIsOutput(BaseModel):
    """Agent-facing kpi_designer schema.

    Requires non-empty content so Strands retries blank structured_output.
    ``min_length``/``max_length`` encode the prompt's stated "4-6 KPIs" /
    "3-5 events". Uses ``BrandHealthKPIOutput`` (not the soft
    ``BrandHealthKPI``) so each KPI's fields are individually required.
    """

    brand_health_kpis: List[BrandHealthKPIOutput] = Field(
        min_length=BRAND_HEALTH_KPIS_MIN, max_length=BRAND_HEALTH_KPIS_MAX
    )
    tracking_methodology: str = Field(min_length=1)
    review_trigger_points: List[NonEmptyStr] = Field(
        min_length=REVIEW_TRIGGER_POINTS_MIN, max_length=REVIEW_TRIGGER_POINTS_MAX
    )


class EvolutionFrameworkOutput(BaseModel):
    """Agent-facing evolution_framer schema.

    Requires non-empty content so Strands retries blank structured_output.
    """

    evolution_framework: str = Field(min_length=1)
    version_control_cadence: str = Field(min_length=1)


class BrandGuidelinesOutput(BaseModel):
    """Agent-facing brand_rules_codifier schema.

    Requires non-empty content so Strands retries blank structured_output.
    ``min_length``/``max_length`` encode the prompt's stated "5-8 governance
    rules".
    """

    brand_guidelines: List[NonEmptyStr] = Field(
        min_length=BRAND_GUIDELINES_MIN, max_length=BRAND_GUIDELINES_MAX
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
    """A single mood-board direction; merge target for ``MoodBoardConceptOutput``."""

    title: str
    visual_direction: str
    color_story: List[str] = Field(default_factory=list)
    typography_direction: str
    image_style: List[str] = Field(default_factory=list)


class CreativeRefinementDecision(BaseModel):
    """Phase 3 converge node output: which moodboard direction won and why."""

    winning_candidate_title: str = ""
    scoring_criteria: List[str] = Field(default_factory=list)
    scores_by_candidate: Dict[str, float] = Field(default_factory=dict)
    rationale: str = ""
    workshop_prompts: List[str] = Field(default_factory=list)
    decision_criteria: List[str] = Field(default_factory=list)


class WritingGuidelines(BaseModel):
    """Voice/tone and editorial rules; merge target for ``WritingGuidelinesOutput``."""

    voice_principles: List[str] = Field(default_factory=list)
    style_dos: List[str] = Field(default_factory=list)
    style_donts: List[str] = Field(default_factory=list)
    editorial_quality_bar: List[str] = Field(default_factory=list)


class DesignSystemDefinition(BaseModel):
    """Codified design system; merge target for ``DesignSystemDefinitionOutput``."""

    design_principles: List[str] = Field(default_factory=list)
    foundation_tokens: List[str] = Field(default_factory=list)
    component_standards: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Phase 3 agent-facing structured_output schemas
# ---------------------------------------------------------------------------
# Merge targets above keep empty defaults so partial fragments validate.
# Agent schemas below require content so Strands retries blank output.


class MoodBoardConceptOutput(BaseModel):
    """Agent-facing moodboard concept schema for MoodBoardConceptualist_*."""

    title: str = Field(min_length=1)
    visual_direction: str = Field(min_length=1)
    color_story: List[str] = Field(min_length=1)
    typography_direction: str = Field(min_length=1)
    image_style: List[str] = Field(min_length=1)


class MoodBoardCandidatesOutput(BaseModel):
    """Agent-facing CreativeDirector schema: collected moodboard candidates.

    ``min_length``/``max_length`` encode the diverge fan-out of 2–3 concepts.
    Nested entries use ``MoodBoardConceptOutput`` so blank concepts fail validation.
    """

    mood_board_candidates: List[MoodBoardConceptOutput] = Field(min_length=2, max_length=3)


class CreativeRefinementDecisionOutput(BaseModel):
    """Agent-facing converge_decider schema.

    Field-for-field twin of ``CreativeRefinementDecision`` with required content.
    """

    winning_candidate_title: str = Field(min_length=1)
    scoring_criteria: List[str] = Field(min_length=1)
    scores_by_candidate: Dict[str, float] = Field(min_length=1)
    rationale: str = Field(min_length=1)
    workshop_prompts: List[str] = Field(min_length=1)
    decision_criteria: List[str] = Field(min_length=1)


class LogoSuiteOutput(BaseModel):
    """Agent-facing logo_specifier schema.

    ``min_length``/``max_length`` encode the prompt's four logo variants.
    """

    logo_suite: List[LogoUsageRuleOutput] = Field(min_length=4, max_length=4)


class ColorPaletteSystemOutput(BaseModel):
    """Agent-facing color_system_builder schema.

    Named to avoid colliding with mission ``ColorPalette``.
    ``min_length``/``max_length`` encode the prompt's stated "5-7 colors".
    """

    color_palette: List[ColorEntryOutput] = Field(
        min_length=COLOR_PALETTE_MIN, max_length=COLOR_PALETTE_MAX
    )


class TypographySystemOutput(BaseModel):
    """Agent-facing typography_builder schema.

    ``min_length``/``max_length`` encode the prompt's stated "3-4 type roles".
    """

    typography_system: List[TypographySpecOutput] = Field(
        min_length=TYPOGRAPHY_SYSTEM_MIN, max_length=TYPOGRAPHY_SYSTEM_MAX
    )


class IconographyOutput(BaseModel):
    """Agent-facing iconography_director schema."""

    iconography_style: str = Field(min_length=1)
    illustration_style: str = Field(min_length=1)


class PhotographyVideoOutput(BaseModel):
    """Agent-facing photography_video_director schema.

    ``motion_principles`` cardinality matches the prompt's stated "3-4 principles".
    """

    photography_direction: str = Field(min_length=1)
    video_direction: str = Field(min_length=1)
    motion_principles: List[str] = Field(
        min_length=MOTION_PRINCIPLES_MIN, max_length=MOTION_PRINCIPLES_MAX
    )


class VoiceToneOutput(BaseModel):
    """Agent-facing voice_tone_builder schema.

    ``language_dos``/``language_donts`` match the prompt's stated "4-5" items.
    """

    voice_tone_spectrum: List[VoiceToneEntryOutput] = Field(min_length=1)
    language_dos: List[str] = Field(min_length=LANGUAGE_DOS_MIN, max_length=LANGUAGE_DOS_MAX)
    language_donts: List[str] = Field(min_length=LANGUAGE_DONTS_MIN, max_length=LANGUAGE_DONTS_MAX)


class DesignSystemDefinitionOutput(BaseModel):
    """Agent-facing design_system_codifier schema.

    Field-for-field twin of ``DesignSystemDefinition`` with required content.
    """

    design_principles: List[str] = Field(min_length=1)
    foundation_tokens: List[str] = Field(min_length=1)
    component_standards: List[str] = Field(min_length=1)


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
