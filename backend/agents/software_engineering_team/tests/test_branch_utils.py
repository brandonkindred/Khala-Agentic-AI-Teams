"""Unit tests for the shared v2 deliver branch-slug helpers."""

from __future__ import annotations

from shared.git import branch_utils


def test_make_slug_sanitizes_and_bounds() -> None:
    """Verify make_slug sanitizes input, bounds length, and falls back safely."""
    assert branch_utils.make_slug("t1", "Build the API!") == "build-the-api"
    # Title preferred over id; bounded to 40 chars.
    assert len(branch_utils.make_slug("t1", "x" * 200)) <= 40
    # Empty inputs fall back to "task".
    assert branch_utils.make_slug("", "") == "task"
    assert branch_utils.make_slug("", "!!!") == "task"


def test_make_task_id_slug_sanitizes_and_bounds() -> None:
    """Verify make_task_id_slug sanitizes task ids and enforces length bounds."""
    assert branch_utils.make_task_id_slug("PROJ/123 fix") == "proj-123-fix"
    assert len(branch_utils.make_task_id_slug("z" * 200)) <= 20
    assert branch_utils.make_task_id_slug("") == "task"


def test_make_branch_suffix_appends_stable_task_id_hash() -> None:
    """Verify branch suffixes append a stable hash for the task id."""
    suffix = branch_utils.make_branch_suffix("api", "Build API")
    assert suffix.startswith("api-build-api-")
    assert len(suffix.rsplit("-", 1)[-1]) == 8
    # Stable for a given task id (retries reuse the same branch).
    assert branch_utils.make_branch_suffix("api", "Build API") == suffix


def test_make_branch_suffix_disambiguates_punctuation_only_collisions() -> None:
    """Verify punctuation-only task-id differences still produce distinct branch suffixes."""
    a = branch_utils.make_branch_suffix("api.v1", "Build API")
    b = branch_utils.make_branch_suffix("api-v1", "Build API")
    # Same human-readable slug, different trailing hash → no branch collision.
    assert a != b
    assert a.startswith("api-v1-build-api-")
    assert b.startswith("api-v1-build-api-")
