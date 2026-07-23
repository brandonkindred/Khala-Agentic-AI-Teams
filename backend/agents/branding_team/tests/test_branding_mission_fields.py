"""Unit tests for branding domain models composition.

Preconditions:
    - ``branding_team.models`` is importable under the test path setup.
Postconditions:
    - Assertions pin shared-field composition for ``BrandingMission``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from branding_team.models import BrandingMission, BrandingMissionFields

SHARED_FIELD_NAMES = (
    "company_name",
    "company_description",
    "target_audience",
    "values",
    "differentiators",
    "desired_voice",
    "existing_brand_material",
    "wiki_path",
)

VISUAL_FIELD_NAMES = (
    "color_inspiration",
    "color_palettes",
    "selected_palette_index",
    "visual_style",
    "typography_preference",
    "interface_density",
)


def test_branding_mission_subclasses_mission_fields() -> None:
    assert issubclass(BrandingMission, BrandingMissionFields)


def test_mission_fields_exposes_exactly_the_eight_shared_fields() -> None:
    assert tuple(BrandingMissionFields.model_fields) == SHARED_FIELD_NAMES


def test_branding_mission_keeps_shared_plus_visual_fields() -> None:
    names = tuple(BrandingMission.model_fields)
    for name in SHARED_FIELD_NAMES:
        assert name in names
    for name in VISUAL_FIELD_NAMES:
        assert name in names
    assert names[: len(SHARED_FIELD_NAMES)] == SHARED_FIELD_NAMES


def test_branding_mission_defaults_and_dump_match_shared_base() -> None:
    mission = BrandingMission(
        company_name="Acme",
        company_description="We build widgets for teams",
        target_audience="B2B buyers",
    )
    dumped = mission.model_dump()
    assert dumped["desired_voice"] == "clear, confident, human"
    assert dumped["values"] == []
    assert dumped["differentiators"] == []
    assert dumped["existing_brand_material"] == []
    assert dumped["wiki_path"] is None
    assert dumped["visual_style"] == ""
    assert dumped["typography_preference"] == ""
    assert dumped["interface_density"] == ""
    assert dumped["color_inspiration"] == []
    assert dumped["color_palettes"] == []
    assert dumped["selected_palette_index"] is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("company_name", "A"),
        ("company_description", "too short"),
        ("target_audience", "ab"),
    ],
)
def test_shared_min_length_constraints_still_reject(field: str, value: str) -> None:
    kwargs = {
        "company_name": "Acme",
        "company_description": "We build widgets for teams",
        "target_audience": "B2B buyers",
        field: value,
    }
    with pytest.raises(ValidationError):
        BrandingMission(**kwargs)


def test_branding_mission_fields_constructs_independently() -> None:
    fields = BrandingMissionFields(
        company_name="Acme",
        company_description="We build widgets for teams",
        target_audience="B2B buyers",
    )
    assert fields.company_name == "Acme"
    assert "color_inspiration" not in fields.model_dump()
