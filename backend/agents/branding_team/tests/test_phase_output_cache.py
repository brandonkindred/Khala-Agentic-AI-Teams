"""Tests for branding_team.shared.phase_output_cache.PhaseOutputCache."""

from __future__ import annotations

import pytest

from branding_team.models import BrandPhase, NarrativeMessagingOutput, StrategicCoreOutput
from branding_team.shared.phase_output_cache import (
    PhaseOutputCache,
    _cache_key,
    _phase_cache_namespace,
)
from shared.cache import get_shared_cache


def test_get_on_empty_cache_is_a_miss() -> None:
    cache = PhaseOutputCache()

    assert cache.get(BrandPhase.STRATEGIC_CORE, "some-hash") is None


def test_put_then_get_with_matching_hash_is_a_hit() -> None:
    cache = PhaseOutputCache()
    output = StrategicCoreOutput(brand_purpose="Ship calm software")

    cache.put(BrandPhase.STRATEGIC_CORE, "hash-a", output)

    assert cache.get(BrandPhase.STRATEGIC_CORE, "hash-a") == output


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


def test_put_for_same_phase_with_different_hash_keeps_both_entries_addressable() -> None:
    """Unlike the old single-slot-per-phase cache, a put under a new hash does
    not evict the prior hash's entry -- keys are content-addressed
    (``phase:input_hash``), so both remain independently retrievable until
    evicted by the shared backend's LRU."""
    cache = PhaseOutputCache()
    first = StrategicCoreOutput(brand_purpose="Ship calm software")
    second = StrategicCoreOutput(brand_purpose="Ship bold software")

    cache.put(BrandPhase.STRATEGIC_CORE, "hash-a", first)
    cache.put(BrandPhase.STRATEGIC_CORE, "hash-b", second)

    assert cache.get(BrandPhase.STRATEGIC_CORE, "hash-a") == first
    assert cache.get(BrandPhase.STRATEGIC_CORE, "hash-b") == second


def test_entries_for_distinct_phases_are_independent() -> None:
    cache = PhaseOutputCache()
    strategic = StrategicCoreOutput(brand_purpose="Ship calm software")
    narrative = NarrativeMessagingOutput(tagline="Calm, on purpose.")

    cache.put(BrandPhase.STRATEGIC_CORE, "hash-a", strategic)
    cache.put(BrandPhase.NARRATIVE_MESSAGING, "hash-b", narrative)

    assert cache.get(BrandPhase.STRATEGIC_CORE, "hash-a") == strategic
    assert cache.get(BrandPhase.NARRATIVE_MESSAGING, "hash-b") == narrative


def test_get_rejects_complete_phase() -> None:
    cache = PhaseOutputCache()

    with pytest.raises(ValueError, match="not a runnable branding phase"):
        cache.get(BrandPhase.COMPLETE, "hash-a")


def test_put_rejects_complete_phase() -> None:
    cache = PhaseOutputCache()
    output = StrategicCoreOutput(brand_purpose="Ship calm software")

    with pytest.raises(ValueError, match="not a runnable branding phase"):
        cache.put(BrandPhase.COMPLETE, "hash-a", output)


def test_corrupt_entry_is_evicted_and_treated_as_a_miss() -> None:
    cache = PhaseOutputCache()
    key = _cache_key(BrandPhase.STRATEGIC_CORE, "hash-a")
    shared_cache = get_shared_cache(_phase_cache_namespace())
    shared_cache.set(key, b"not valid json", max_entries=64)

    assert cache.get(BrandPhase.STRATEGIC_CORE, "hash-a") is None
    # Eviction actually happened -- a fresh put now lands cleanly.
    output = StrategicCoreOutput(brand_purpose="Ship calm software")
    cache.put(BrandPhase.STRATEGIC_CORE, "hash-a", output)
    assert cache.get(BrandPhase.STRATEGIC_CORE, "hash-a") == output


def test_two_instances_share_the_same_backing_cache() -> None:
    """PhaseOutputCache wraps a process-wide, namespaced shared.cache -- it is
    no longer a private per-instance dict, so a second instance sees the
    first's entries."""
    writer = PhaseOutputCache()
    reader = PhaseOutputCache()
    output = StrategicCoreOutput(brand_purpose="Ship calm software")

    writer.put(BrandPhase.STRATEGIC_CORE, "hash-a", output)

    assert reader.get(BrandPhase.STRATEGIC_CORE, "hash-a") == output
