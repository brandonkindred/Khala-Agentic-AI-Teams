"""Structured-output model for the branding chat assistant's silent extractor.

``MissionUpdate`` is the schema-validated shape of what the extractor stage
(``branding_team.assistant.agent``) is allowed to emit each turn: an optional
per-field delta against the current ``BrandingMission``, plus the next
suggested follow-up questions. Every field is optional because a turn where
the user shares nothing new must still validate — "nothing learned this turn"
is a legitimate, all-``None``/empty payload, not an error.

Field set is deliberately kept in lockstep with
``branding_team.assistant.agent``'s ``_MISSION_STR_FIELDS`` /
``_MISSION_LIST_FIELDS`` / ``_MISSION_STRUCTURED_FIELDS`` tuples (the
extractor's own single source of truth for which ``BrandingMission`` fields
it reads/writes) plus ``suggested_questions``. Wiring this model into the
extraction agent's ``structured_output=`` is out of scope here — see the
parent epic.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field

from branding_team.models import ColorPalette


class MissionUpdate(BaseModel):
    """One turn's worth of extracted ``BrandingMission`` deltas.

    Every field mirrors a chat-editable ``BrandingMission`` field (or, for
    ``suggested_questions``, the extractor's own follow-up-question output)
    and is ``Optional`` so an all-empty payload — the extractor found nothing
    new this turn — validates cleanly. Every field declares a non-blank
    ``Field(description=...)`` so this model can serve directly as an
    ``AgentPromptSpec.structured_output`` (see ``prompt_spec.py``'s
    ``_field_lines_from_model``, which asserts exactly that).

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

    company_name: Optional[str] = Field(
        default=None, description="the company or product name, if newly learned or changed"
    )
    company_description: Optional[str] = Field(
        default=None,
        description="a sentence or two on what the company does and for whom, if newly learned",
    )
    target_audience: Optional[str] = Field(
        default=None, description="the primary target audience, if newly learned or changed"
    )
    desired_voice: Optional[str] = Field(
        default=None, description="the brand's desired voice/tone, if newly learned or changed"
    )
    visual_style: Optional[str] = Field(
        default=None,
        description="the desired visual style (e.g. minimalist, maximalist), if newly learned",
    )
    typography_preference: Optional[str] = Field(
        default=None,
        description="the desired typography direction (e.g. geometric sans-serif), if newly learned",
    )
    interface_density: Optional[str] = Field(
        default=None,
        description="the desired interface density (e.g. spacious, dense), if newly learned",
    )
    values: Optional[List[str]] = Field(
        default=None,
        description="the complete current list of brand values the user has shared so far",
    )
    differentiators: Optional[List[str]] = Field(
        default=None,
        description="the complete current list of competitive differentiators shared so far",
    )
    existing_brand_material: Optional[List[str]] = Field(
        default=None,
        description="the complete current list of existing brand material the user has referenced",
    )
    color_inspiration: Optional[List[str]] = Field(
        default=None,
        description="the complete current list of color inspiration references shared so far",
    )
    color_palettes: Optional[List[ColorPalette]] = Field(
        default=None,
        description="candidate color palettes presented to the user for selection, if any",
    )
    selected_palette_index: Optional[int] = Field(
        default=None,
        description="the index into color_palettes the user selected, or null if none/unselected",
    )
    suggested_questions: Optional[List[str]] = Field(
        default=None,
        description="up to a few natural-language follow-up questions to ask the user next",
    )
