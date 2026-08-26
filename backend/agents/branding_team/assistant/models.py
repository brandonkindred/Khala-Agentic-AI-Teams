"""Structured-output model for the branding chat assistant's silent extractor.

``MissionUpdate`` is the schema-validated shape of what the extractor stage
(``branding_team.assistant.agent``) is allowed to emit each turn: an optional
per-field delta against the current ``BrandingMission``, plus the next
suggested follow-up questions. Every field is optional because a turn where
the user shares nothing new must still validate — "nothing learned this turn"
is a legitimate, all-``None``/empty payload, not an error.

Mission fields are inherited from ``branding_team.models``'s
``_optionalize_model`` (the same helper ``api.models._BrandingMissionFieldsPartial``
uses) applied to ``BrandingMission``, rather than redeclared here, so this
schema cannot silently drift from the canonical mission model or from
``assistant.agent``'s ``_MISSION_STR_FIELDS``/``_MISSION_LIST_FIELDS``/
``_MISSION_STRUCTURED_FIELDS`` (which enumerate the same field set). Wiring
this model into the extraction agent's ``structured_output=`` is out of
scope here — see the parent epic.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import Field

from branding_team.models import BrandingMission, _optionalize_model

# Field-for-field descriptions for the ``_optionalize_model`` twin below —
# new content (``BrandingMission``'s own fields carry no descriptions), not a
# duplicate of the field list itself. Keys must match
# ``BrandingMission.model_fields`` minus ``wiki_path`` exactly (asserted by
# ``_optionalize_model``).
_MISSION_UPDATE_FIELD_DESCRIPTIONS: Dict[str, str] = {
    "company_name": "the company or product name, if newly learned or changed",
    "company_description": (
        "a sentence or two on what the company does and for whom, if newly learned"
    ),
    "target_audience": "the primary target audience, if newly learned or changed",
    "values": "the complete current list of brand values the user has shared so far",
    "differentiators": "the complete current list of competitive differentiators shared so far",
    "desired_voice": "the brand's desired voice/tone, if newly learned or changed",
    "existing_brand_material": (
        "the complete current list of existing brand material the user has referenced"
    ),
    "color_inspiration": "the complete current list of color inspiration references shared so far",
    "color_palettes": "candidate color palettes presented to the user for selection, if any",
    "selected_palette_index": (
        "the index into color_palettes the user selected, or null if none/unselected"
    ),
    "visual_style": "the desired visual style (e.g. minimalist, maximalist), if newly learned",
    "typography_preference": (
        "the desired typography direction (e.g. geometric sans-serif), if newly learned"
    ),
    "interface_density": ("the desired interface density (e.g. spacious, dense), if newly learned"),
}

# ``wiki_path`` is excluded: it's a pipeline-populated link to the brand's
# wiki page, not a field the chat assistant's extractor reads/writes.
_MissionUpdateFields = _optionalize_model(
    BrandingMission,
    name="_MissionUpdateFields",
    exclude=frozenset({"wiki_path"}),
    descriptions=_MISSION_UPDATE_FIELD_DESCRIPTIONS,
)


class MissionUpdate(_MissionUpdateFields):
    """One turn's worth of the branding chat extractor's ``BrandingMission`` deltas.

    Mission fields are inherited from ``_MissionUpdateFields`` — an
    ``_optionalize_model`` twin of ``BrandingMission`` (minus ``wiki_path``,
    a pipeline-only field) — rather than redeclared here, so this schema
    cannot silently drift from the canonical mission model. ``suggested_questions``
    is the extractor's own additional output, added here since it has no
    ``BrandingMission`` counterpart to derive from.

    Every field — inherited and own — is ``Optional`` so an all-empty
    payload (the extractor found nothing new this turn) validates cleanly,
    and every field declares a non-blank ``Field(description=...)`` so this
    model can serve directly as an ``AgentPromptSpec.structured_output``
    (see ``prompt_spec.py``'s ``_field_lines_from_model``, which asserts
    exactly that).

    Preconditions:
        ``selected_palette_index``, when not ``None``, is a plain ``int``
        (index bounds-checking against ``color_palettes`` is the merge
        layer's responsibility, not this schema's — the same division of
        labor ``BrandingMission`` itself uses).
    Postconditions:
        Constructing with no arguments (or with every field explicitly
        ``None``/empty) succeeds and represents "nothing learned this turn".
        Any subset of fields may be populated independently of the others.
    """

    suggested_questions: Optional[List[str]] = Field(
        default=None,
        description="up to a few natural-language follow-up questions to ask the user next",
    )
