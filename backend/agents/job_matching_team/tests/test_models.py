"""Tests for domain models and helpers."""

from __future__ import annotations

from job_matching_team.models import JobPosting, compute_fingerprint


def test_fingerprint_is_stable_across_formatting():
    a = compute_fingerprint("Acme  Inc.", "Senior  Engineer", "Remote, US")
    b = compute_fingerprint("acme inc", "senior engineer", "remote us")
    assert a == b
    assert len(a) == 16


def test_fingerprint_differs_on_distinct_roles():
    a = compute_fingerprint("Acme", "Engineer", "NYC")
    b = compute_fingerprint("Acme", "Manager", "NYC")
    assert a != b


def test_ensure_fingerprint_populates_once():
    p = JobPosting(company="Acme", title="Engineer", location="NYC")
    assert p.fingerprint == ""
    p.ensure_fingerprint()
    fp = p.fingerprint
    assert fp
    # Idempotent: a second call does not change an existing fingerprint.
    p.title = "Changed"
    p.ensure_fingerprint()
    assert p.fingerprint == fp


def test_listing_defaults_to_new_status():
    from job_matching_team.models import Listing

    listing = Listing(fingerprint="fp1", posting=JobPosting(company="Acme", title="Eng"))
    assert listing.status == "new"
    assert listing.times_seen == 1
    assert listing.notes is None


def test_listing_rejects_invalid_status():
    import pytest

    from job_matching_team.models import Listing, ListingStateUpdate

    with pytest.raises(Exception):
        Listing(fingerprint="fp1", posting=JobPosting(), status="starred")
    with pytest.raises(Exception):
        ListingStateUpdate(status="starred")


def test_listing_state_update_notes_optional():
    from job_matching_team.models import ListingStateUpdate

    update = ListingStateUpdate(status="favorite")
    assert update.notes is None
    update = ListingStateUpdate(status="poor_fit", notes="below salary floor")
    assert update.notes == "below salary floor"


def test_listing_filters_cover_every_status():
    from job_matching_team.models import LISTING_FILTERS

    # Every ListingStatus value must be filterable, plus the two pseudo-filters.
    assert set(LISTING_FILTERS) == {
        "active",
        "all",
        "new",
        "favorite",
        "not_interested",
        "poor_fit",
        "archived",
    }
