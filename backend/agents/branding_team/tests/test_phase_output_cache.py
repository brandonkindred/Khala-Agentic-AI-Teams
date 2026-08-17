"""Tests for branding_team.shared.phase_output_cache.PhaseOutputCache."""

from __future__ import annotations

import pytest

from branding_team.models import BrandPhase, NarrativeMessagingOutput, StrategicCoreOutput
from branding_team.shared.phase_output_cache import PhaseOutputCache


def test_get_on_empty_cache_is_a_miss() -> None:
    cache = PhaseOutputCache()

    assert cache.get(BrandPhase.STRATEGIC_CORE, "some-hash") is None


def test_put_then_get_with_matching_hash_is_a_hit() -> None:
    cache = PhaseOutputCache()
    output = StrategicCoreOutput(brand_purpose="Ship calm software")

    cache.put(BrandPhase.STRATEGIC_CORE, "hash-a", output)

    assert cache.get(BrandPhase.STRATEGIC_CORE, "hash-a") is output


def test_get_with_mismatched_hash_is_a_miss() -> None:
    cache = PhaseOutputCache()
    output = StrategicCoreOutput(brand_purpose="Ship calm software")

    cache.put(BrandPhase.STRATEGIC_CORE, "hash-a", output)

    assert cache.get(BrandPhase.STRATEGIC_CORE, "hash-b") is None


def test_different_phases_do_not_collide() -> None:
    cache = PhaseOutputCache()
    output = StrategicCoreOutput(brand_purpose="Ship calm software")

    cache.put(BrandPhase.STRATEGIC_CORE, "hash-a", output)

    assert cache.get(BrandPhase.NARRATIVE_MESSAGING, "hash-a") is None


def test_second_put_overwrites_prior_entry_for_same_phase() -> None:
    cache = PhaseOutputCache()
    first = StrategicCoreOutput(brand_purpose="Ship calm software")
    second = StrategicCoreOutput(brand_purpose="Ship bold software")

    cache.put(BrandPhase.STRATEGIC_CORE, "hash-a", first)
    cache.put(BrandPhase.STRATEGIC_CORE, "hash-b", second)

    assert cache.get(BrandPhase.STRATEGIC_CORE, "hash-a") is None
    assert cache.get(BrandPhase.STRATEGIC_CORE, "hash-b") is second


def test_entries_for_distinct_phases_are_independent() -> None:
    cache = PhaseOutputCache()
    strategic = StrategicCoreOutput(brand_purpose="Ship calm software")
    narrative = NarrativeMessagingOutput(tagline="Calm, on purpose.")

    cache.put(BrandPhase.STRATEGIC_CORE, "hash-a", strategic)
    cache.put(BrandPhase.NARRATIVE_MESSAGING, "hash-b", narrative)

    assert cache.get(BrandPhase.STRATEGIC_CORE, "hash-a") is strategic
    assert cache.get(BrandPhase.NARRATIVE_MESSAGING, "hash-b") is narrative


def test_get_rejects_complete_phase() -> None:
    cache = PhaseOutputCache()

    with pytest.raises(ValueError, match="not a runnable branding phase"):
        cache.get(BrandPhase.COMPLETE, "hash-a")


def test_put_rejects_complete_phase() -> None:
    cache = PhaseOutputCache()
    output = StrategicCoreOutput(brand_purpose="Ship calm software")

    with pytest.raises(ValueError, match="not a runnable branding phase"):
        cache.put(BrandPhase.COMPLETE, "hash-a", output)
