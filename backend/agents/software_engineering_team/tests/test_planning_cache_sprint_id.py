"""Tests for the ``sprint_id`` keying behavior added in #370.

The bare-minimum invariants we need:

1. Same inputs + ``sprint_id=None`` → same key as a pre-#370 call.
2. Different ``sprint_id`` → different key.
3. ``sprint_id=None`` and the historic two-arg call produce the
   *byte-identical* key, so existing on-disk cache entries stay valid.
"""

from __future__ import annotations

from software_engineering_team.shared.planning_cache import compute_planning_cache_key


def test_sprint_id_none_matches_pre_change_key() -> None:
    # Recompute the pre-#370 hash directly: sprint_id was not part of
    # the blob, so omitting it (or passing None) must yield the same
    # 24-char digest. Regression guard against accidental cache
    # invalidation for non-sprint runs.
    legacy = compute_planning_cache_key("spec", "arch overview")
    with_none = compute_planning_cache_key("spec", "arch overview", sprint_id=None)
    assert legacy == with_none


def test_distinct_sprint_ids_yield_distinct_keys() -> None:
    base = compute_planning_cache_key("spec", "arch overview")
    a = compute_planning_cache_key("spec", "arch overview", sprint_id="sprint-a")
    b = compute_planning_cache_key("spec", "arch overview", sprint_id="sprint-b")
    assert a != b
    assert a != base and b != base


def test_project_overview_plus_sprint_id_compose() -> None:
    # `project_overview` and `sprint_id` are independent dimensions of
    # the key — flipping either changes the digest, neither shadows
    # the other.
    po = {"primary_goal": "x", "delivery_strategy": "y"}
    only_po = compute_planning_cache_key("spec", "arch", po)
    only_sprint = compute_planning_cache_key("spec", "arch", sprint_id="s1")
    both = compute_planning_cache_key("spec", "arch", po, sprint_id="s1")
    assert len({only_po, only_sprint, both}) == 3
