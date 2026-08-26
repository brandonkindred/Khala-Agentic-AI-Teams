"""Validation tests for ``MissionUpdate``, the branding chat extractor's schema.

Covers the three payload shapes the extractor can legitimately produce each
turn: nothing learned (all-empty), a partial update (some fields set), and a
fully-populated update (every field set) — see ``MissionUpdate``'s docstring
for why an all-empty payload must validate rather than error.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from branding_team.assistant.models import MissionUpdate
from branding_team.models import ColorPalette


def test_all_empty_payload_validates() -> None:
    update = MissionUpdate()

    for field in MissionUpdate.model_fields:
        assert getattr(update, field) is None


def test_all_empty_payload_validates_explicit_none() -> None:
    update = MissionUpdate.model_validate({field: None for field in MissionUpdate.model_fields})

    for field in MissionUpdate.model_fields:
        assert getattr(update, field) is None


def test_partial_payload_only_sets_given_fields() -> None:
    update = MissionUpdate.model_validate(
        {
            "company_name": "Acme",
            "target_audience": "Developers",
            "values": ["curiosity", "craft"],
        }
    )

    assert update.company_name == "Acme"
    assert update.target_audience == "Developers"
    assert update.values == ["curiosity", "craft"]

    untouched_fields = set(MissionUpdate.model_fields) - {
        "company_name",
        "target_audience",
        "values",
    }
    for field in untouched_fields:
        assert getattr(update, field) is None


def test_fully_populated_payload_validates() -> None:
    payload = {
        "company_name": "Acme",
        "company_description": "Acme builds developer tools that ship faster.",
        "target_audience": "Backend engineers at mid-size startups",
        "desired_voice": "clear, confident, human",
        "visual_style": "minimalist",
        "typography_preference": "geometric sans-serif",
        "interface_density": "spacious",
        "values": ["curiosity", "craft", "candor"],
        "differentiators": ["fastest onboarding", "best docs"],
        "existing_brand_material": ["current logo", "pitch deck"],
        "color_inspiration": ["deep blue", "warm gray"],
        "color_palettes": [
            {
                "name": "Midnight",
                "description": "cool and professional",
                "colors": ["#0B1F3A", "#1D3557"],
                "sentiment": "cool and professional",
            }
        ],
        "selected_palette_index": 0,
        "suggested_questions": ["Who are your top three competitors?"],
    }

    update = MissionUpdate.model_validate(payload)

    assert update.company_name == "Acme"
    assert update.company_description == payload["company_description"]
    assert update.target_audience == payload["target_audience"]
    assert update.desired_voice == "clear, confident, human"
    assert update.visual_style == "minimalist"
    assert update.typography_preference == "geometric sans-serif"
    assert update.interface_density == "spacious"
    assert update.values == ["curiosity", "craft", "candor"]
    assert update.differentiators == ["fastest onboarding", "best docs"]
    assert update.existing_brand_material == ["current logo", "pitch deck"]
    assert update.color_inspiration == ["deep blue", "warm gray"]
    assert update.color_palettes == [ColorPalette(**payload["color_palettes"][0])]
    assert update.selected_palette_index == 0
    assert update.suggested_questions == ["Who are your top three competitors?"]


def test_selected_palette_index_rejects_non_int() -> None:
    with pytest.raises(ValidationError):
        MissionUpdate.model_validate({"selected_palette_index": "first"})


def test_every_field_declares_a_non_blank_description() -> None:
    for name, field_info in MissionUpdate.model_fields.items():
        assert field_info.description and field_info.description.strip(), (
            f"MissionUpdate.{name} must declare a non-blank Field(description=...)"
        )


def test_field_set_matches_mission_and_suggestions_fields() -> None:
    from branding_team.assistant.agent import (
        _MISSION_LIST_FIELDS,
        _MISSION_STR_FIELDS,
        _MISSION_STRUCTURED_FIELDS,
    )

    expected = (
        set(_MISSION_STR_FIELDS)
        | set(_MISSION_LIST_FIELDS)
        | set(_MISSION_STRUCTURED_FIELDS)
        | {"suggested_questions"}
    )
    assert set(MissionUpdate.model_fields) == expected
