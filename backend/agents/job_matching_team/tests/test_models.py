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
