"""Unit tests for branding domain models composition.

Preconditions:
    - ``branding_team.models`` and ``branding_team.api.models`` are importable
      under the test path setup.
Postconditions:
    - Assertions pin shared-field composition for ``BrandingMission`` and
      ``CreateBrandRequest``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from branding_team.api.models import CreateBrandRequest
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

CREATE_BRAND_EXTRA_FIELD_NAMES = (
    "name",
    "conversation_id",
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


def test_create_brand_request_subclasses_mission_fields() -> None:
    assert issubclass(CreateBrandRequest, BrandingMissionFields)


def test_create_brand_request_keeps_shared_plus_api_extra_fields() -> None:
    names = tuple(CreateBrandRequest.model_fields)
    for name in SHARED_FIELD_NAMES:
        assert name in names
    for name in CREATE_BRAND_EXTRA_FIELD_NAMES:
        assert name in names
    assert names[: len(SHARED_FIELD_NAMES)] == SHARED_FIELD_NAMES


def test_create_brand_request_validation_and_defaults_match_shared_base() -> None:
    req = CreateBrandRequest(
        company_name="Acme",
        company_description="We build widgets for teams",
        target_audience="B2B buyers",
    )
    dumped = req.model_dump()
    assert dumped["desired_voice"] == "clear, confident, human"
    assert dumped["values"] == []
    assert dumped["differentiators"] == []
    assert dumped["existing_brand_material"] == []
    assert dumped["wiki_path"] is None
    assert dumped["name"] is None
    assert dumped["conversation_id"] is None
    with pytest.raises(ValidationError):
        CreateBrandRequest(
            company_name="A",
            company_description="We build widgets for teams",
            target_audience="B2B buyers",
        )


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


def test_mission_placeholders_tuple_contents() -> None:
    from branding_team.models import (
        MISSION_PLACEHOLDER_TBD,
        MISSION_PLACEHOLDER_TO_BE_DISCUSSED,
        MISSION_PLACEHOLDERS,
    )

    assert MISSION_PLACEHOLDER_TBD == "TBD"
    assert MISSION_PLACEHOLDER_TO_BE_DISCUSSED == "To be discussed."
    assert MISSION_PLACEHOLDERS == (
        MISSION_PLACEHOLDER_TBD,
        MISSION_PLACEHOLDER_TO_BE_DISCUSSED,
        "—",
        "",
    )


def test_default_mission_and_detection_use_shared_placeholders() -> None:
    from branding_team.api.state import _is_real_value
    from branding_team.assistant.store import _default_mission
    from branding_team.models import (
        MISSION_PLACEHOLDER_TBD,
        MISSION_PLACEHOLDER_TO_BE_DISCUSSED,
        MISSION_PLACEHOLDERS,
    )

    mission = _default_mission()
    assert mission.company_name == MISSION_PLACEHOLDER_TBD
    assert mission.company_description == MISSION_PLACEHOLDER_TO_BE_DISCUSSED
    assert mission.target_audience == MISSION_PLACEHOLDER_TBD
    for sentinel in MISSION_PLACEHOLDERS:
        assert _is_real_value(sentinel) is False
    assert _is_real_value("Acme Corp") is True
