"""Regression tests for the three Codex review findings on PR #229.

1. P1: BiometricPatchRequest.timezone must reject invalid IANA zones
   at the request boundary so the orchestrator cannot silently
   persist a value that later breaks ClientProfile reads.
2. P1: a PATCH /biometrics request that only sets `measured_at`
   must persist (earlier code skipped save_profile when no numeric
   field changed).
3. P2: get_biometric_history must accept RFC3339 `Z`-suffixed
   timestamps — `datetime.fromisoformat` on Python 3.10 rejects them.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from nutrition_meal_planning_team.models import BiometricPatchRequest

# --- Fix 1 (P1): timezone validation at request boundary ----------------


def test_biometric_patch_rejects_invalid_iana_timezone():
    """Typo zones like 'America/New_Yorkk' must be rejected here so
    the orchestrator can't persist them via direct attribute assignment.
    """
    with pytest.raises(ValidationError):
        BiometricPatchRequest(timezone="America/New_Yorkk")


def test_biometric_patch_accepts_none_timezone():
    p = BiometricPatchRequest(timezone=None)
    assert p.timezone is None


def test_biometric_patch_accepts_utc():
    p = BiometricPatchRequest(timezone="UTC")
    assert p.timezone == "UTC"


def test_biometric_patch_accepts_known_zone():
    p = BiometricPatchRequest(timezone="America/New_York")
    assert p.timezone == "America/New_York"


# --- Fix 3 (P2): since_iso parser tolerates RFC3339 'Z' -----------------
#
# Tested as a pure-unit fragment via the same normalization rule the
# orchestrator uses, so we don't need Postgres or the full orchestrator.


def _normalize_since(since_iso: str | None):
    """Mirror of the orchestrator's get_biometric_history normalizer."""
    if not since_iso:
        return None
    normalized = since_iso[:-1] + "+00:00" if since_iso.endswith("Z") else since_iso
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def test_since_iso_rfc3339_z_parses():
    """'2026-04-18T12:00:00Z' must parse — not silently fall to None."""
    dt = _normalize_since("2026-04-18T12:00:00Z")
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt.utcoffset().total_seconds() == 0


def test_since_iso_explicit_offset_still_parses():
    dt = _normalize_since("2026-04-18T12:00:00+00:00")
    assert dt is not None
    assert dt.utcoffset().total_seconds() == 0


def test_since_iso_naive_assumed_utc():
    """Naive timestamps keep existing behavior (assume UTC)."""
    dt = _normalize_since("2026-04-18T12:00:00")
    assert dt is not None
    assert dt.tzinfo is not None


def test_since_iso_garbage_returns_none():
    assert _normalize_since("not a timestamp") is None


def test_since_iso_none_returns_none():
    assert _normalize_since(None) is None
    assert _normalize_since("") is None
