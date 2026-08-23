"""Tests for branding_team.shared.memoization.phase_input_hash."""

from __future__ import annotations

import pytest

from branding_team.models import BrandPhase, NarrativeMessagingOutput, StrategicCoreOutput
from branding_team.shared.memoization import phase_input_hash
from branding_team.tests.conftest import make_mission


def test_equal_inputs_produce_identical_hash_across_calls() -> None:
    mission = make_mission()
    upstream = {BrandPhase.STRATEGIC_CORE: StrategicCoreOutput(brand_purpose="Ship calm software")}

    first = phase_input_hash(BrandPhase.NARRATIVE_MESSAGING, mission, upstream)
    second = phase_input_hash(BrandPhase.NARRATIVE_MESSAGING, mission, upstream)

    assert first == second
    assert len(first) == 64
    int(first, 16)  # digest is valid lowercase hex


def test_equal_but_distinct_instances_produce_identical_hash() -> None:
    upstream_a = {
        BrandPhase.STRATEGIC_CORE: StrategicCoreOutput(brand_purpose="Ship calm software")
    }
    upstream_b = {
        BrandPhase.STRATEGIC_CORE: StrategicCoreOutput(brand_purpose="Ship calm software")
    }

    hash_a = phase_input_hash(BrandPhase.NARRATIVE_MESSAGING, make_mission(), upstream_a)
    hash_b = phase_input_hash(BrandPhase.NARRATIVE_MESSAGING, make_mission(), upstream_b)

    assert hash_a == hash_b


def test_upstream_outputs_insertion_order_does_not_affect_hash() -> None:
    mission = make_mission()
    strategic = StrategicCoreOutput(brand_purpose="Ship calm software")
    narrative = NarrativeMessagingOutput(tagline="Calm, on purpose.")

    forward = {BrandPhase.STRATEGIC_CORE: strategic, BrandPhase.NARRATIVE_MESSAGING: narrative}
    reversed_order = {
        BrandPhase.NARRATIVE_MESSAGING: narrative,
        BrandPhase.STRATEGIC_CORE: strategic,
    }

    assert phase_input_hash(BrandPhase.VISUAL_IDENTITY, mission, forward) == phase_input_hash(
        BrandPhase.VISUAL_IDENTITY, mission, reversed_order
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"company_name": "Different Co"},
        {"values": ["clarity", "trust", "tech", "speed"]},
    ],
)
def test_changed_mission_field_changes_hash(overrides: dict) -> None:
    baseline = phase_input_hash(BrandPhase.STRATEGIC_CORE, make_mission(), {})
    changed = phase_input_hash(BrandPhase.STRATEGIC_CORE, make_mission(**overrides), {})

    assert baseline != changed


def test_changed_upstream_output_field_changes_hash() -> None:
    mission = make_mission()
    baseline = phase_input_hash(
        BrandPhase.NARRATIVE_MESSAGING,
        mission,
        {BrandPhase.STRATEGIC_CORE: StrategicCoreOutput(brand_purpose="Ship calm software")},
    )
    changed = phase_input_hash(
        BrandPhase.NARRATIVE_MESSAGING,
        mission,
        {BrandPhase.STRATEGIC_CORE: StrategicCoreOutput(brand_purpose="Ship bold software")},
    )

    assert baseline != changed


def test_added_upstream_output_entry_changes_hash() -> None:
    mission = make_mission()
    strategic = StrategicCoreOutput(brand_purpose="Ship calm software")

    without_narrative = phase_input_hash(
        BrandPhase.VISUAL_IDENTITY, mission, {BrandPhase.STRATEGIC_CORE: strategic}
    )
    with_narrative = phase_input_hash(
        BrandPhase.VISUAL_IDENTITY,
        mission,
        {
            BrandPhase.STRATEGIC_CORE: strategic,
            BrandPhase.NARRATIVE_MESSAGING: NarrativeMessagingOutput(tagline="Calm, on purpose."),
        },
    )

    assert without_narrative != with_narrative


def test_different_phase_changes_hash_for_identical_mission_and_upstream() -> None:
    mission = make_mission()

    strategic_hash = phase_input_hash(BrandPhase.STRATEGIC_CORE, mission, {})
    narrative_hash = phase_input_hash(BrandPhase.NARRATIVE_MESSAGING, mission, {})

    assert strategic_hash != narrative_hash


def test_empty_upstream_outputs_is_valid_for_first_phase() -> None:
    digest = phase_input_hash(BrandPhase.STRATEGIC_CORE, make_mission(), {})

    assert len(digest) == 64


def test_complete_phase_is_rejected() -> None:
    with pytest.raises(ValueError, match="not a runnable branding phase"):
        phase_input_hash(BrandPhase.COMPLETE, make_mission(), {})


def test_empty_context_phases_matches_omitted_context_phases() -> None:
    """Passing ``context_phases=()`` explicitly reproduces the default hash."""
    mission = make_mission()
    upstream = {BrandPhase.STRATEGIC_CORE: StrategicCoreOutput(brand_purpose="Ship calm software")}

    default = phase_input_hash(BrandPhase.NARRATIVE_MESSAGING, mission, upstream)
    explicit = phase_input_hash(BrandPhase.NARRATIVE_MESSAGING, mission, upstream, ())

    assert default == explicit


def test_context_phases_excludes_unlisted_upstream_output_from_hash() -> None:
    """An ``upstream_outputs`` entry not named in ``context_phases`` has no
    effect on the hash, even though it is present in the mapping."""
    mission = make_mission()
    strategic = StrategicCoreOutput(brand_purpose="Ship calm software")

    without_channel = phase_input_hash(
        BrandPhase.GOVERNANCE,
        mission,
        {BrandPhase.STRATEGIC_CORE: strategic},
        context_phases=(BrandPhase.STRATEGIC_CORE,),
    )
    with_irrelevant_channel = phase_input_hash(
        BrandPhase.GOVERNANCE,
        mission,
        {
            BrandPhase.STRATEGIC_CORE: strategic,
            BrandPhase.NARRATIVE_MESSAGING: NarrativeMessagingOutput(tagline="Calm, on purpose."),
        },
        context_phases=(BrandPhase.STRATEGIC_CORE,),
    )

    assert without_channel == with_irrelevant_channel


def test_context_phases_change_to_excluded_upstream_output_does_not_change_hash() -> None:
    """A field change on an upstream output excluded by ``context_phases``
    does not change the digest -- the selective-hashing counterpart to
    ``test_changed_upstream_output_field_changes_hash``."""
    mission = make_mission()
    context_phases = (BrandPhase.STRATEGIC_CORE,)

    baseline = phase_input_hash(
        BrandPhase.VISUAL_IDENTITY,
        mission,
        {
            BrandPhase.STRATEGIC_CORE: StrategicCoreOutput(brand_purpose="Ship calm software"),
            BrandPhase.NARRATIVE_MESSAGING: NarrativeMessagingOutput(tagline="Calm, on purpose."),
        },
        context_phases=context_phases,
    )
    changed = phase_input_hash(
        BrandPhase.VISUAL_IDENTITY,
        mission,
        {
            BrandPhase.STRATEGIC_CORE: StrategicCoreOutput(brand_purpose="Ship calm software"),
            BrandPhase.NARRATIVE_MESSAGING: NarrativeMessagingOutput(tagline="Different tagline."),
        },
        context_phases=context_phases,
    )

    assert baseline == changed


def test_context_phases_change_to_included_upstream_output_changes_hash() -> None:
    """A field change on an upstream output included by ``context_phases``
    still changes the digest."""
    mission = make_mission()
    context_phases = (BrandPhase.STRATEGIC_CORE,)

    baseline = phase_input_hash(
        BrandPhase.NARRATIVE_MESSAGING,
        mission,
        {BrandPhase.STRATEGIC_CORE: StrategicCoreOutput(brand_purpose="Ship calm software")},
        context_phases=context_phases,
    )
    changed = phase_input_hash(
        BrandPhase.NARRATIVE_MESSAGING,
        mission,
        {BrandPhase.STRATEGIC_CORE: StrategicCoreOutput(brand_purpose="Ship bold software")},
        context_phases=context_phases,
    )

    assert baseline != changed
