"""Unit tests for branding domain models composition.

Preconditions:
    - ``branding_team.models`` and ``branding_team.api.models`` are importable
      under the test path setup.
Postconditions:
    - Assertions pin shared-field composition for ``BrandingMission``,
      ``CreateBrandRequest``, ``UpdateBrandRequest``, and
      ``RunBrandingTeamRequest``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from branding_team.api.models import (
    CreateBrandRequest,
    RunBrandingTeamRequest,
    UpdateBrandRequest,
)
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

UPDATE_BRAND_EXTRA_FIELD_NAMES = (
    "name",
    "status",
)

RUN_BRANDING_TEAM_EXTRA_FIELD_NAMES = (
    "brand_checks",
    "human_approved",
    "human_feedback",
    "client_id",
    "brand_id",
    "target_phase",
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


def test_run_branding_team_request_subclasses_mission_fields() -> None:
    assert issubclass(RunBrandingTeamRequest, BrandingMissionFields)


def test_run_branding_team_request_keeps_shared_plus_run_extra_fields() -> None:
    names = tuple(RunBrandingTeamRequest.model_fields)
    for name in SHARED_FIELD_NAMES:
        assert name in names
    for name in RUN_BRANDING_TEAM_EXTRA_FIELD_NAMES:
        assert name in names
    assert names[: len(SHARED_FIELD_NAMES)] == SHARED_FIELD_NAMES


def test_run_branding_team_request_validation_and_defaults_match_shared_base() -> None:
    req = RunBrandingTeamRequest(
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
    assert dumped["brand_checks"] == []
    assert dumped["human_approved"] is False
    assert dumped["human_feedback"] == ""
    assert dumped["client_id"] is None
    assert dumped["brand_id"] is None
    assert dumped["target_phase"] is None
    with pytest.raises(ValidationError):
        RunBrandingTeamRequest(
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


def test_mission_fields_method_returns_exactly_shared_keys_and_values() -> None:
    fields = BrandingMissionFields(
        company_name="Acme",
        company_description="We build widgets for teams",
        target_audience="B2B buyers",
        values=["clarity"],
        differentiators=["speed"],
        desired_voice="warm",
        existing_brand_material=["logo.svg"],
        wiki_path="/wiki/acme",
    )
    dumped = fields.mission_fields()
    assert tuple(dumped.keys()) == SHARED_FIELD_NAMES
    assert dumped == {
        "company_name": "Acme",
        "company_description": "We build widgets for teams",
        "target_audience": "B2B buyers",
        "values": ["clarity"],
        "differentiators": ["speed"],
        "desired_voice": "warm",
        "existing_brand_material": ["logo.svg"],
        "wiki_path": "/wiki/acme",
    }


def test_mission_fields_method_omits_create_brand_api_extras() -> None:
    req = CreateBrandRequest(
        company_name="Acme",
        company_description="We build widgets for teams",
        target_audience="B2B buyers",
        name="Display Name",
        conversation_id="conv-1",
    )
    dumped = req.mission_fields()
    assert tuple(dumped.keys()) == SHARED_FIELD_NAMES
    assert "name" not in dumped
    assert "conversation_id" not in dumped


def test_mission_fields_method_omits_run_request_api_extras() -> None:
    req = RunBrandingTeamRequest(
        company_name="Acme",
        company_description="We build widgets for teams",
        target_audience="B2B buyers",
        human_approved=True,
        client_id="c1",
        brand_id="b1",
        target_phase="strategic_core",
    )
    dumped = req.mission_fields()
    assert tuple(dumped.keys()) == SHARED_FIELD_NAMES
    assert "human_approved" not in dumped
    assert "client_id" not in dumped
    assert "brand_id" not in dumped
    assert "target_phase" not in dumped
    assert "brand_checks" not in dumped
    assert "human_feedback" not in dumped


def test_mission_from_payload_builds_mission_from_shared_fields_only() -> None:
    from branding_team.api.state import _mission_from_payload

    req = CreateBrandRequest(
        company_name="Acme",
        company_description="We build widgets for teams",
        target_audience="B2B buyers",
        values=["clarity"],
        name="Display Name",
        conversation_id="conv-1",
    )
    mission = _mission_from_payload(req)
    assert isinstance(mission, BrandingMission)
    assert mission.company_name == "Acme"
    assert mission.company_description == "We build widgets for teams"
    assert mission.target_audience == "B2B buyers"
    assert mission.values == ["clarity"]
    assert mission.desired_voice == "clear, confident, human"
    assert mission.visual_style == ""
    assert mission.color_inspiration == []
    assert mission.selected_palette_index is None
    assert "name" not in BrandingMission.model_fields
    assert "conversation_id" not in BrandingMission.model_fields


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


def test_is_real_value_none_and_whitespace_are_not_real() -> None:
    from branding_team.api.state import _is_real_value

    assert _is_real_value(None) is False
    assert _is_real_value("   ") is False


def test_update_brand_request_includes_shared_and_extra_fields() -> None:
    names = tuple(UpdateBrandRequest.model_fields)
    for name in SHARED_FIELD_NAMES:
        assert name in names
    for name in UPDATE_BRAND_EXTRA_FIELD_NAMES:
        assert name in names


def test_update_brand_request_mission_fields_default_to_none() -> None:
    req = UpdateBrandRequest()
    dumped = req.model_dump()
    for name in SHARED_FIELD_NAMES:
        assert dumped[name] is None
    assert dumped["name"] is None
    assert dumped["status"] is None


def test_update_brand_request_rejects_short_company_name_when_supplied() -> None:
    with pytest.raises(ValidationError):
        UpdateBrandRequest(company_name="A")


def test_update_brand_request_partial_dump_excludes_none_mission_fields() -> None:
    req = UpdateBrandRequest(company_description="Updated description here")
    patch = req.model_dump(exclude_none=True, exclude={"status", "name"})
    assert patch == {"company_description": "Updated description here"}


def test_update_brand_request_mission_fields_come_from_optionalized_base() -> None:
    """Mission fields must be inherited from the generated partial, not redeclared."""
    from branding_team.api import models as api_models

    partial = api_models._BrandingMissionFieldsPartial
    assert issubclass(UpdateBrandRequest, partial)
    assert tuple(partial.model_fields) == SHARED_FIELD_NAMES
