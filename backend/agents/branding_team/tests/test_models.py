"""Validation tests for the Phase 1 structured-output wrapper models.

These agent-facing models (``PurposeVisionOutput``, ``CoreValuesOutput``,
``AudienceSegmentsOutput``, ``DifferentiationPillarsOutput``,
``PositioningOutput``) must reject empty/omitted content so Strands'
structured-output tool retries the LLM instead of silently accepting a blank
or under-cardinality response (see ``structured_output_tool.py``: a
``ValidationError`` becomes a tool error the model is asked to fix).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from branding_team.models import (
    AudienceSegment,
    AudienceSegmentsOutput,
    CoreValue,
    CoreValuesOutput,
    DifferentiationPillar,
    DifferentiationPillarsOutput,
    PositioningOutput,
    PurposeVisionOutput,
)


def test_purpose_vision_output_rejects_missing_and_empty_fields() -> None:
    with pytest.raises(ValidationError):
        PurposeVisionOutput()
    with pytest.raises(ValidationError):
        PurposeVisionOutput(brand_purpose="", mission_statement="x", vision_statement="x")

    output = PurposeVisionOutput(brand_purpose="a", mission_statement="b", vision_statement="c")
    assert output.brand_purpose == "a"


def test_positioning_output_rejects_missing_and_empty_fields() -> None:
    with pytest.raises(ValidationError):
        PositioningOutput()
    with pytest.raises(ValidationError):
        PositioningOutput(positioning_statement="", brand_promise="x")

    output = PositioningOutput(positioning_statement="x", brand_promise="y")
    assert output.brand_promise == "y"


def test_core_values_output_enforces_stated_cardinality() -> None:
    """Prompt asks for "3-5 core values"."""
    value = CoreValue(value="clarity")
    with pytest.raises(ValidationError):
        CoreValuesOutput(core_values=[value, value])  # below min of 3
    with pytest.raises(ValidationError):
        CoreValuesOutput(core_values=[value] * 6)  # above max of 5

    output = CoreValuesOutput(core_values=[value] * 3)
    assert len(output.core_values) == 3


def test_audience_segments_output_enforces_stated_cardinality() -> None:
    """Prompt asks for "1-3 target audience segments"."""
    segment = AudienceSegment(name="Enterprise leaders")
    with pytest.raises(ValidationError):
        AudienceSegmentsOutput(target_audience_segments=[])  # below min of 1
    with pytest.raises(ValidationError):
        AudienceSegmentsOutput(target_audience_segments=[segment] * 4)  # above max of 3

    output = AudienceSegmentsOutput(target_audience_segments=[segment])
    assert len(output.target_audience_segments) == 1


def test_differentiation_pillars_output_enforces_stated_cardinality() -> None:
    """Prompt asks for "2-4 differentiation pillars"."""
    pillar = DifferentiationPillar(pillar="Execution speed")
    with pytest.raises(ValidationError):
        DifferentiationPillarsOutput(differentiation_pillars=[pillar])  # below min of 2
    with pytest.raises(ValidationError):
        DifferentiationPillarsOutput(differentiation_pillars=[pillar] * 5)  # above max of 4

    output = DifferentiationPillarsOutput(differentiation_pillars=[pillar] * 2)
    assert len(output.differentiation_pillars) == 2
