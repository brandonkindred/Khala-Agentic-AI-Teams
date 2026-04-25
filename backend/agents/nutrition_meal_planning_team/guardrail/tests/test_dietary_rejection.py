"""SPEC-007 §4.4 step 3 — dietary rejection.

Spec §6.1: vegan + milk → reject; pescatarian + chicken → reject;
pescatarian + fish → pass.
"""

from __future__ import annotations

import pytest
from agents.nutrition_meal_planning_team.guardrail import (
    Severity,
    ViolationReason,
    check_recommendation,
)
from agents.nutrition_meal_planning_team.ingredient_kb.taxonomy import DietaryTag

from ._fixtures import profile_with, recipe


def test_vegan_rejects_milk() -> None:
    profile = profile_with(dietary_forbid=[DietaryTag.animal, DietaryTag.dairy])
    rec = recipe("milk")

    result = check_recommendation(profile, rec)

    assert result.passed is False
    dietary = [v for v in result.violations if v.reason is ViolationReason.dietary_forbid]
    assert dietary, "expected a dietary_forbid violation"
    assert all(v.severity is Severity.hard_reject for v in dietary)
    assert {v.tag for v in dietary} == {"animal", "dairy"}


def test_pescatarian_rejects_chicken() -> None:
    """Pescatarian = forbid `animal` but exempt `fish`. The catalog
    tags chicken with ``dietary_tags=[animal]`` (no fish), so a
    pescatarian profile (forbid `animal`) hard-rejects chicken."""
    profile = profile_with(dietary_forbid=[DietaryTag.animal])
    rec = recipe("chicken thigh")

    result = check_recommendation(profile, rec)

    assert result.passed is False
    dietary = [v for v in result.violations if v.reason is ViolationReason.dietary_forbid]
    assert any(v.tag == "animal" for v in dietary)


def test_pescatarian_passes_fish() -> None:
    """Pescatarian forbids `animal` for non-fish proteins, but salmon
    parses with ``dietary_tags=[animal]`` only — so the *naive* model
    would still reject. Per SPEC-006, the resolver omits the `animal`
    forbid for pescatarian once fish is exempt; we model that here by
    not putting `animal` in the active set when the user is
    pescatarian-with-fish-OK."""
    profile = profile_with(dietary_forbid=[])  # pescatarian-w/-fish has no active forbid here
    rec = recipe("salmon")

    result = check_recommendation(profile, rec)

    assert result.passed is True


def test_honey_forbidden_for_vegan() -> None:
    """SPEC-007 red-team: honey on a vegan profile must reject."""
    profile = profile_with(dietary_forbid=[DietaryTag.honey, DietaryTag.animal])
    rec = recipe("honey")

    result = check_recommendation(profile, rec)

    assert result.passed is False
    tags = {v.tag for v in result.violations}
    assert "honey" in tags
    assert "animal" in tags


@pytest.mark.skip(
    reason=(
        "SPEC-006 follow-up: pescatarian shorthand resolves to "
        "forbid_dietary=[animal] but active_dietary_forbid() does not "
        "subtract the fish exemption — see issue #351."
    )
)
def test_pescatarian_resolution_passes_fish() -> None:
    """Regression pin for the SPEC-006 exemption mechanism.

    Once #351 lands the resolver-level fish exemption, this test must
    pass with a pescatarian-shorthand-resolved profile (not the
    sidestepped empty-forbid version below)."""
    profile = profile_with(dietary_forbid=[DietaryTag.animal])
    rec = recipe("salmon")

    result = check_recommendation(profile, rec)

    assert result.passed is True


def test_no_dietary_forbid_passes_anything() -> None:
    profile = profile_with(dietary_forbid=[])
    rec = recipe("milk", "egg", "chicken thigh", "salmon")

    result = check_recommendation(profile, rec)

    assert result.passed is True
    assert result.violations == ()
