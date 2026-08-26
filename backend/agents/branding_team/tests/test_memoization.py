"""Tests for branding_team.shared.memoization.phase_input_hash."""

from __future__ import annotations

import pytest

from branding_team.models import (
    BrandPhase,
    ChannelActivationOutput,
    NarrativeMessagingOutput,
    StrategicCoreOutput,
    VisualIdentityOutput,
)
from branding_team.orchestrator import _PHASE_SPEC
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


def test_omitted_context_phases_matches_explicit_none() -> None:
    """Omitting ``context_phases`` reproduces the hash of passing ``None``
    explicitly -- ``None`` is the true default sentinel, not just a
    falsy-equivalent placeholder."""
    mission = make_mission()
    upstream = {BrandPhase.STRATEGIC_CORE: StrategicCoreOutput(brand_purpose="Ship calm software")}

    omitted = phase_input_hash(BrandPhase.NARRATIVE_MESSAGING, mission, upstream)
    explicit_none = phase_input_hash(BrandPhase.NARRATIVE_MESSAGING, mission, upstream, None)

    assert omitted == explicit_none


def test_explicit_empty_context_phases_excludes_everything() -> None:
    """``context_phases=()`` deliberately means "no upstream context" and
    must differ from the ``None``/omitted default when ``upstream_outputs``
    is non-empty -- the None-vs-() distinction this function exists to make
    expressible."""
    mission = make_mission()
    upstream = {BrandPhase.STRATEGIC_CORE: StrategicCoreOutput(brand_purpose="Ship calm software")}

    include_all = phase_input_hash(BrandPhase.NARRATIVE_MESSAGING, mission, upstream)
    include_none = phase_input_hash(BrandPhase.NARRATIVE_MESSAGING, mission, upstream, ())
    empty_upstream = phase_input_hash(BrandPhase.NARRATIVE_MESSAGING, mission, {})

    assert include_all != include_none
    assert include_none == empty_upstream


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


def test_phase4_output_change_invalidates_phase5_cache() -> None:
    """Phase 5's real ``context_phases`` is unset (no filtering), so a Phase 4
    edit must change Phase 5's hash (cache miss) -- the wired-up-config
    counterpart to ``test_context_phases_change_to_included_upstream_output_changes_hash``."""
    mission = make_mission()
    context_phases = _PHASE_SPEC[BrandPhase.GOVERNANCE].context_phases
    strategic = StrategicCoreOutput(brand_purpose="Ship calm software")
    visual = VisualIdentityOutput(iconography_style="Geometric")

    baseline = phase_input_hash(
        BrandPhase.GOVERNANCE,
        mission,
        {
            BrandPhase.STRATEGIC_CORE: strategic,
            BrandPhase.VISUAL_IDENTITY: visual,
            BrandPhase.CHANNEL_ACTIVATION: ChannelActivationOutput(naming_conventions=["Alpha"]),
        },
        context_phases=context_phases,
    )
    changed = phase_input_hash(
        BrandPhase.GOVERNANCE,
        mission,
        {
            BrandPhase.STRATEGIC_CORE: strategic,
            BrandPhase.VISUAL_IDENTITY: visual,
            BrandPhase.CHANNEL_ACTIVATION: ChannelActivationOutput(naming_conventions=["Beta"]),
        },
        context_phases=context_phases,
    )

    assert baseline != changed


@pytest.mark.parametrize(
    "downstream_phase",
    [
        BrandPhase.NARRATIVE_MESSAGING,
        BrandPhase.VISUAL_IDENTITY,
        BrandPhase.CHANNEL_ACTIVATION,
        BrandPhase.GOVERNANCE,
    ],
)
def test_phase1_output_change_invalidates_all_downstream_phase_hashes(
    downstream_phase: BrandPhase,
) -> None:
    """Every downstream phase's real ``context_phases`` includes Phase 1, so a
    Phase 1 edit must change every downstream phase's hash (cache miss)."""
    mission = make_mission()
    other_upstream = {
        BrandPhase.NARRATIVE_MESSAGING: NarrativeMessagingOutput(tagline="Calm, on purpose."),
        BrandPhase.VISUAL_IDENTITY: VisualIdentityOutput(iconography_style="Geometric"),
    }
    context_phases = _PHASE_SPEC[downstream_phase].context_phases

    baseline = phase_input_hash(
        downstream_phase,
        mission,
        {
            BrandPhase.STRATEGIC_CORE: StrategicCoreOutput(brand_purpose="Ship calm software"),
            **other_upstream,
        },
        context_phases=context_phases,
    )
    changed = phase_input_hash(
        downstream_phase,
        mission,
        {
            BrandPhase.STRATEGIC_CORE: StrategicCoreOutput(brand_purpose="Ship bold software"),
            **other_upstream,
        },
        context_phases=context_phases,
    )

    assert baseline != changed


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


def test_omitted_mission_fields_matches_explicit_none() -> None:
    """Omitting ``mission_fields`` reproduces the hash of passing ``None``
    explicitly -- ``None`` is the true default sentinel, not just a
    falsy-equivalent placeholder."""
    mission = make_mission()

    omitted = phase_input_hash(BrandPhase.STRATEGIC_CORE, mission, {})
    explicit_none = phase_input_hash(BrandPhase.STRATEGIC_CORE, mission, {}, None, None)

    assert omitted == explicit_none


def test_omitted_mission_fields_reproduces_pre_change_digest() -> None:
    """A call using only the pre-#7349 positional signature (no
    ``mission_fields`` argument at all) must reproduce the exact digest this
    function produced before ``mission_fields`` existed -- no silent
    behavior change for un-migrated phases. This pins a known-good digest
    computed against ``make_mission()``'s defaults."""
    digest = phase_input_hash(BrandPhase.STRATEGIC_CORE, make_mission(), {})

    assert digest == "bce443cab64f45a10a8043d1c92e2b69919517582475ea394427de25b1e08e9f"


@pytest.mark.parametrize(
    "allowlisted_field,overrides",
    [
        ("company_name", {"company_name": "Different Co"}),
        ("values", {"values": ["clarity", "trust", "tech", "speed"]}),
    ],
)
def test_allowlisted_mission_field_change_changes_hash(
    allowlisted_field: str, overrides: dict
) -> None:
    """A change to a mission field named in ``mission_fields`` changes the
    digest."""
    mission_fields = frozenset({allowlisted_field})

    baseline = phase_input_hash(BrandPhase.STRATEGIC_CORE, make_mission(), {}, None, mission_fields)
    changed = phase_input_hash(
        BrandPhase.STRATEGIC_CORE, make_mission(**overrides), {}, None, mission_fields
    )

    assert baseline != changed


def test_non_allowlisted_mission_field_change_does_not_change_hash() -> None:
    """A change to a mission field NOT named in ``mission_fields`` does not
    change the digest -- the selective-hashing counterpart to
    ``test_changed_mission_field_changes_hash``."""
    mission_fields = frozenset({"company_name"})

    baseline = phase_input_hash(BrandPhase.STRATEGIC_CORE, make_mission(), {}, None, mission_fields)
    changed = phase_input_hash(
        BrandPhase.STRATEGIC_CORE,
        make_mission(values=["clarity", "trust", "tech", "speed"]),
        {},
        None,
        mission_fields,
    )

    assert baseline == changed


def test_explicit_empty_mission_fields_excludes_every_mission_field() -> None:
    """``mission_fields=frozenset()`` deliberately means "no mission field is
    relevant" and must differ from the ``None``/omitted default -- the
    None-vs-empty-frozenset distinction this parameter exists to make
    expressible."""
    baseline = phase_input_hash(BrandPhase.STRATEGIC_CORE, make_mission(), {})
    empty_allowlist = phase_input_hash(
        BrandPhase.STRATEGIC_CORE, make_mission(), {}, None, frozenset()
    )
    empty_allowlist_changed_mission = phase_input_hash(
        BrandPhase.STRATEGIC_CORE,
        make_mission(company_name="Different Co"),
        {},
        None,
        frozenset(),
    )

    assert baseline != empty_allowlist
    assert empty_allowlist == empty_allowlist_changed_mission


def test_mission_fields_set_order_does_not_affect_hash() -> None:
    """``mission_fields`` built with the same entries in different
    construction order hashes identically -- frozensets are unordered but
    the digest must still be canonical."""
    mission = make_mission()

    a = phase_input_hash(
        BrandPhase.STRATEGIC_CORE,
        mission,
        {},
        None,
        frozenset({"company_name", "values"}),
    )
    b = phase_input_hash(
        BrandPhase.STRATEGIC_CORE,
        mission,
        {},
        None,
        frozenset({"values", "company_name"}),
    )

    assert a == b
