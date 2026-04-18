"""SPEC-004 W2: structured-output schemas for the narrator."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nutrition_meal_planning_team.agents.nutritionist_agent import (
    GuidanceOnlyPayload,
    NarrativePayload,
)

# --- NarrativePayload ----------------------------------------------------


def test_narrative_all_empty_is_valid():
    p = NarrativePayload()
    assert p.balance_guidelines == []
    assert p.foods_to_emphasize == []
    assert p.foods_to_avoid == []
    assert p.notes == ""
    assert p.summary == ""


def test_narrative_populated_roundtrip():
    p = NarrativePayload(
        balance_guidelines=["eat more veg"],
        foods_to_emphasize=["leafy greens"],
        foods_to_avoid=["deep-fried foods"],
        notes="focus on protein spread",
        summary="Mediterranean-leaning maintenance plan.",
    )
    dumped = p.model_dump()
    restored = NarrativePayload.model_validate(dumped)
    assert restored == p


def test_narrative_rejects_extra_fields():
    """extra='forbid' blocks schema-busting LLM output."""
    with pytest.raises(ValidationError):
        NarrativePayload.model_validate({"balance_guidelines": [], "calories_kcal": 2000})


# --- GuidanceOnlyPayload -------------------------------------------------


def test_guidance_requires_clinician_note():
    """clinician_note is required on guidance-only — an empty string is NOT valid."""
    with pytest.raises(ValidationError):
        GuidanceOnlyPayload()  # no clinician_note
    with pytest.raises(ValidationError):
        GuidanceOnlyPayload(clinician_note="")


def test_guidance_minimal_valid():
    p = GuidanceOnlyPayload(clinician_note="Please work with your clinician.")
    assert p.balance_guidelines == []
    assert p.clinician_note.startswith("Please work")


def test_guidance_rejects_extra_fields():
    with pytest.raises(ValidationError):
        GuidanceOnlyPayload.model_validate(
            {
                "clinician_note": "work with clinician",
                "calories_kcal": 1800,
            }
        )
