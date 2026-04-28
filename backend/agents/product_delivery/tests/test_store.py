"""Integration tests for :class:`ProductDeliveryStore` against live Postgres.

Skipped automatically when ``POSTGRES_HOST`` is unset (matches the
pattern used by ``agent_console`` / ``shared_postgres`` tests).
"""

from __future__ import annotations

import pytest

from product_delivery.postgres import SCHEMA
from product_delivery.store import (
    CrossProductFeedbackLink,
    ProductDeliveryStore,
    StoryAlreadyPlanned,
    UnknownProductDeliveryEntity,
    get_store,
)
from shared_postgres import is_postgres_enabled, register_team_schemas
from shared_postgres.testing import truncate_team_tables

pytestmark = pytest.mark.skipif(
    not is_postgres_enabled(),
    reason="POSTGRES_HOST not set; skipping live-Postgres store tests",
)


@pytest.fixture(autouse=True)
def _provision_schema() -> None:
    register_team_schemas(SCHEMA)
    truncate_team_tables(SCHEMA)


def _store() -> ProductDeliveryStore:
    return get_store()


def test_create_product_round_trip() -> None:
    store = _store()
    p = store.create_product(name="P", description="d", vision="v", author="alice")
    assert p.author == "alice"
    again = store.get_product(p.id)
    assert again is not None
    assert again.name == "P"


def test_full_hierarchy_creation_and_backlog_tree() -> None:
    store = _store()
    p = store.create_product(name="P", description="", vision="", author="alice")
    i = store.create_initiative(
        product_id=p.id, title="I", summary="", status="proposed", author="alice"
    )
    e = store.create_epic(
        initiative_id=i.id, title="E", summary="", status="proposed", author="alice"
    )
    s = store.create_story(
        epic_id=e.id,
        title="S",
        user_story="",
        status="proposed",
        estimate_points=5,
        author="alice",
    )
    store.create_task(
        story_id=s.id,
        title="T",
        description="",
        status="todo",
        owner=None,
        author="alice",
    )
    store.create_acceptance_criterion(
        story_id=s.id, text="must work", satisfied=False, author="alice"
    )

    tree = store.get_backlog_tree(p.id)
    assert tree is not None
    assert tree.product.id == p.id
    assert tree.initiatives[0].id == i.id
    epic = tree.initiatives[0].epics[0]
    assert epic.id == e.id
    story = epic.stories[0]
    assert story.id == s.id
    assert len(story.tasks) == 1
    assert len(story.acceptance_criteria) == 1


def test_initiative_create_raises_when_product_missing() -> None:
    store = _store()
    with pytest.raises(UnknownProductDeliveryEntity):
        store.create_initiative(
            product_id="missing",
            title="x",
            summary="",
            status="proposed",
            author="alice",
        )


def test_status_and_score_updates() -> None:
    store = _store()
    p = store.create_product(name="P", description="", vision="", author="alice")
    i = store.create_initiative(
        product_id=p.id, title="I", summary="", status="proposed", author="alice"
    )
    assert store.update_status(kind="initiative", entity_id=i.id, status="in_sprint")
    assert store.update_scores(kind="initiative", entity_id=i.id, wsjf_score=12.5, rice_score=80.0)
    tree = store.get_backlog_tree(p.id)
    assert tree is not None
    assert tree.initiatives[0].status == "in_sprint"
    assert tree.initiatives[0].wsjf_score == 12.5
    assert tree.initiatives[0].rice_score == 80.0


def test_bulk_update_story_scores_only_persists_specified_columns() -> None:
    store = _store()
    p = store.create_product(name="P", description="", vision="", author="alice")
    i = store.create_initiative(
        product_id=p.id, title="I", summary="", status="proposed", author="alice"
    )
    e = store.create_epic(
        initiative_id=i.id, title="E", summary="", status="proposed", author="alice"
    )
    s = store.create_story(
        epic_id=e.id,
        title="S",
        user_story="",
        status="proposed",
        estimate_points=None,
        author="alice",
    )

    n = store.bulk_update_story_scores([(s.id, 4.2, None)])
    assert n == 1

    flat = store.list_stories_for_product(p.id)
    assert flat[0].wsjf_score == 4.2
    assert flat[0].rice_score is None


def test_feedback_round_trip_and_status_filter() -> None:
    store = _store()
    p = store.create_product(name="P", description="", vision="", author="alice")
    f = store.create_feedback_item(
        product_id=p.id,
        source="bug-tracker",
        raw_payload={"issue": 123},
        severity="high",
        linked_story_id=None,
        author="alice",
    )
    open_items = store.list_feedback(p.id, status="open")
    assert len(open_items) == 1
    assert open_items[0].id == f.id

    closed = store.list_feedback(p.id, status="closed")
    assert closed == []


def test_get_backlog_tree_batches_child_reads() -> None:
    # Regression for the N+1 fan-out: with two epics, four stories, and
    # a task + AC per story, the request should hit the DB exactly five
    # times (product, initiatives, epics, stories, tasks, ACs) — not
    # 1 + 1 + (1 per epic) + (1 per story) * 2.
    store = _store()
    p = store.create_product(name="P", description="", vision="", author="alice")
    i = store.create_initiative(
        product_id=p.id, title="I", summary="", status="proposed", author="alice"
    )
    epic_ids: list[str] = []
    story_ids: list[str] = []
    for n in range(2):
        e = store.create_epic(
            initiative_id=i.id,
            title=f"E{n}",
            summary="",
            status="proposed",
            author="alice",
        )
        epic_ids.append(e.id)
        for k in range(2):
            s = store.create_story(
                epic_id=e.id,
                title=f"S{n}-{k}",
                user_story="",
                status="proposed",
                estimate_points=None,
                author="alice",
            )
            story_ids.append(s.id)
            store.create_task(
                story_id=s.id,
                title="T",
                description="",
                status="todo",
                owner=None,
                author="alice",
            )
            store.create_acceptance_criterion(
                story_id=s.id, text="must work", satisfied=False, author="alice"
            )

    tree = store.get_backlog_tree(p.id)
    assert tree is not None
    assert tree.product.id == p.id
    flat_epic_ids = [e.id for e in tree.initiatives[0].epics]
    flat_story_ids = [s.id for e in tree.initiatives[0].epics for s in e.stories]
    assert sorted(flat_epic_ids) == sorted(epic_ids)
    assert sorted(flat_story_ids) == sorted(story_ids)
    # Every story has its task + AC attached (no cross-leakage from the
    # bucketing pass).
    for e in tree.initiatives[0].epics:
        for s in e.stories:
            assert len(s.tasks) == 1
            assert len(s.acceptance_criteria) == 1


def test_get_backlog_tree_returns_empty_initiatives_for_product_without_any() -> None:
    store = _store()
    p = store.create_product(name="P", description="", vision="", author="alice")
    tree = store.get_backlog_tree(p.id)
    assert tree is not None
    assert tree.initiatives == []


def test_feedback_rejects_cross_product_story_link() -> None:
    store = _store()
    p_a = store.create_product(name="A", description="", vision="", author="alice")
    p_b = store.create_product(name="B", description="", vision="", author="alice")
    i_b = store.create_initiative(
        product_id=p_b.id, title="I", summary="", status="proposed", author="alice"
    )
    e_b = store.create_epic(
        initiative_id=i_b.id, title="E", summary="", status="proposed", author="alice"
    )
    s_b = store.create_story(
        epic_id=e_b.id,
        title="S",
        user_story="",
        status="proposed",
        estimate_points=None,
        author="alice",
    )

    with pytest.raises(CrossProductFeedbackLink):
        store.create_feedback_item(
            product_id=p_a.id,
            source="qa",
            raw_payload={},
            severity="normal",
            linked_story_id=s_b.id,
            author="alice",
        )


# ---------------------------------------------------------------------------
# Sprints (Phase 2 of #243)
# ---------------------------------------------------------------------------


def _seed_stories(store: ProductDeliveryStore, *, scores: list[tuple[float | None, float | None]]):
    """Seed a product with one story per (wsjf, points) tuple.

    Scores are applied via ``update_scores`` after create — the public
    ``create_story`` API doesn't take ``wsjf_score`` directly, just like
    in production.
    """
    p = store.create_product(name="P", description="", vision="", author="alice")
    i = store.create_initiative(
        product_id=p.id, title="I", summary="", status="proposed", author="alice"
    )
    e = store.create_epic(
        initiative_id=i.id, title="E", summary="", status="proposed", author="alice"
    )
    out = []
    for wsjf, pts in scores:
        s = store.create_story(
            epic_id=e.id,
            title=f"story-{len(out)}",
            user_story="",
            status="proposed",
            estimate_points=pts,
            author="alice",
        )
        if wsjf is not None:
            store.update_scores(kind="story", entity_id=s.id, wsjf_score=wsjf, rice_score=None)
        out.append(s)
    return p, out


def test_sprint_crud_round_trip() -> None:
    store = _store()
    p = store.create_product(name="P", description="", vision="", author="alice")
    sprint = store.create_sprint(
        product_id=p.id,
        name="S1",
        capacity_points=13.0,
        starts_at=None,
        ends_at=None,
        status="planned",
        author="alice",
    )
    assert sprint.capacity_points == 13.0
    again = store.get_sprint(sprint.id)
    assert again is not None and again.id == sprint.id

    # Status updates work.
    assert store.update_sprint_status(sprint_id=sprint.id, status="active")
    assert (store.get_sprint(sprint.id) or sprint).status == "active"

    # Listing scopes to the parent product.
    listed = store.list_sprints_for_product(p.id)
    assert [s.id for s in listed] == [sprint.id]


def test_add_story_to_sprint_is_idempotent() -> None:
    store = _store()
    p, [s] = _seed_stories(store, scores=[(5.0, 3)])
    sprint = store.create_sprint(
        product_id=p.id,
        name="S1",
        capacity_points=5.0,
        starts_at=None,
        ends_at=None,
        status="planned",
        author="alice",
    )
    assert store.add_story_to_sprint(sprint_id=sprint.id, story_id=s.id) is True
    # Re-add: no-op (returns False, no exception).
    assert store.add_story_to_sprint(sprint_id=sprint.id, story_id=s.id) is False
    assert store.list_planned_story_ids(sprint.id) == [s.id]


def test_select_sprint_scope_zero_capacity_skips_everything() -> None:
    store = _store()
    p, [s] = _seed_stories(store, scores=[(5.0, 1)])
    sprint = store.create_sprint(
        product_id=p.id,
        name="S0",
        capacity_points=0.0,
        starts_at=None,
        ends_at=None,
        status="planned",
        author="alice",
    )
    result = store.select_sprint_scope(sprint_id=sprint.id, capacity_points=0.0)
    assert result.selected_story_ids == []
    assert s.id in result.skipped_story_ids
    assert result.used_capacity == 0.0


def test_select_sprint_scope_skips_oversize_story_then_takes_smaller() -> None:
    store = _store()
    p, [huge, small] = _seed_stories(
        store,
        scores=[(9.0, 100), (1.0, 1)],
    )
    sprint = store.create_sprint(
        product_id=p.id,
        name="S1",
        capacity_points=5.0,
        starts_at=None,
        ends_at=None,
        status="planned",
        author="alice",
    )
    result = store.select_sprint_scope(sprint_id=sprint.id, capacity_points=5.0)
    # Greedy rolls past the oversize highest-WSJF story.
    assert result.selected_story_ids == [small.id]
    assert huge.id in result.skipped_story_ids


def test_select_sprint_scope_ties_broken_by_created_at() -> None:
    store = _store()
    # Two stories with identical WSJF — tie-break is created_at ASC, so
    # the first-created story wins when capacity only fits one.
    p, [first, second] = _seed_stories(store, scores=[(5.0, 3), (5.0, 3)])
    sprint = store.create_sprint(
        product_id=p.id,
        name="S1",
        capacity_points=3.0,
        starts_at=None,
        ends_at=None,
        status="planned",
        author="alice",
    )
    result = store.select_sprint_scope(sprint_id=sprint.id, capacity_points=3.0)
    assert result.selected_story_ids == [first.id]
    assert second.id in result.skipped_story_ids


def test_select_sprint_scope_null_wsjf_ordered_last() -> None:
    store = _store()
    # One scored story + one unscored: scored picks first, unscored
    # would be skipped if capacity ran out. Capacity 100 → both fit and
    # the order proves NULLS LAST.
    p, [scored, unscored] = _seed_stories(store, scores=[(7.0, 1), (None, 1)])
    sprint = store.create_sprint(
        product_id=p.id,
        name="S1",
        capacity_points=100.0,
        starts_at=None,
        ends_at=None,
        status="planned",
        author="alice",
    )
    result = store.select_sprint_scope(sprint_id=sprint.id, capacity_points=100.0)
    assert result.selected_story_ids == [scored.id, unscored.id]


def test_select_sprint_scope_null_estimate_points_treated_as_zero() -> None:
    store = _store()
    p, [unestimated, sized] = _seed_stories(store, scores=[(9.0, None), (5.0, 3)])
    sprint = store.create_sprint(
        product_id=p.id,
        name="S1",
        capacity_points=3.0,
        starts_at=None,
        ends_at=None,
        status="planned",
        author="alice",
    )
    result = store.select_sprint_scope(sprint_id=sprint.id, capacity_points=3.0)
    # Unestimated counts as 0 → both fit inside a 3-point budget.
    assert set(result.selected_story_ids) == {unestimated.id, sized.id}
    assert result.used_capacity == 3.0


def test_select_sprint_scope_excludes_stories_already_planned() -> None:
    store = _store()
    p, [s_a, s_b] = _seed_stories(store, scores=[(9.0, 2), (8.0, 2)])
    s1 = store.create_sprint(
        product_id=p.id,
        name="S1",
        capacity_points=2.0,
        starts_at=None,
        ends_at=None,
        status="planned",
        author="alice",
    )
    plan1 = store.select_sprint_scope(sprint_id=s1.id, capacity_points=2.0)
    assert plan1.selected_story_ids == [s_a.id]

    s2 = store.create_sprint(
        product_id=p.id,
        name="S2",
        capacity_points=100.0,
        starts_at=None,
        ends_at=None,
        status="planned",
        author="alice",
    )
    plan2 = store.select_sprint_scope(sprint_id=s2.id, capacity_points=100.0)
    # Story already in S1 must not appear in either bucket of S2.
    assert s_a.id not in plan2.selected_story_ids
    assert s_a.id not in plan2.skipped_story_ids
    assert s_b.id in plan2.selected_story_ids


def test_get_sprint_with_stories_orders_by_wsjf_desc_nulls_last() -> None:
    store = _store()
    p, stories = _seed_stories(
        store,
        scores=[(1.0, 1), (9.0, 1), (None, 1), (5.0, 1)],
    )
    sprint = store.create_sprint(
        product_id=p.id,
        name="S1",
        capacity_points=100.0,
        starts_at=None,
        ends_at=None,
        status="planned",
        author="alice",
    )
    store.select_sprint_scope(sprint_id=sprint.id, capacity_points=100.0)
    view = store.get_sprint_with_stories(sprint.id)
    assert view is not None
    assert [s.title for s in view.stories[:2]] == ["story-1", "story-3"]  # WSJF 9.0, 5.0
    assert view.stories[-1].title == "story-2"  # NULL last


def test_release_crud_round_trip() -> None:
    store = _store()
    p = store.create_product(name="P", description="", vision="", author="alice")
    sprint = store.create_sprint(
        product_id=p.id,
        name="S1",
        capacity_points=5.0,
        starts_at=None,
        ends_at=None,
        status="planned",
        author="alice",
    )
    rel = store.create_release(
        sprint_id=sprint.id,
        version="v0.1.0",
        notes_path=None,
        shipped_at=None,
        author="alice",
    )
    assert rel.version == "v0.1.0"
    assert store.get_release(rel.id) is not None
    assert [r.id for r in store.list_releases_for_sprint(sprint.id)] == [rel.id]


def test_create_release_404_for_unknown_sprint() -> None:
    store = _store()
    with pytest.raises(UnknownProductDeliveryEntity):
        store.create_release(
            sprint_id="missing",
            version="v0.0.1",
            notes_path=None,
            shipped_at=None,
            author="alice",
        )


def test_add_story_to_sprint_rejects_cross_sprint_double_plan() -> None:
    """Schema-level UNIQUE(story_id) enforces one-sprint-per-story.

    Two ``add_story_to_sprint`` calls into different sprints with the
    same story id should raise ``StoryAlreadyPlanned`` (mapped to 409
    at the route). Prevents the race window Codex flagged on PR #396.
    """
    store = _store()
    p, [s] = _seed_stories(store, scores=[(5.0, 1)])
    s1 = store.create_sprint(
        product_id=p.id,
        name="S1",
        capacity_points=5.0,
        starts_at=None,
        ends_at=None,
        status="planned",
        author="alice",
    )
    s2 = store.create_sprint(
        product_id=p.id,
        name="S2",
        capacity_points=5.0,
        starts_at=None,
        ends_at=None,
        status="planned",
        author="alice",
    )
    assert store.add_story_to_sprint(sprint_id=s1.id, story_id=s.id) is True
    with pytest.raises(StoryAlreadyPlanned):
        store.add_story_to_sprint(sprint_id=s2.id, story_id=s.id)
    # The original assignment is intact.
    assert store.list_planned_story_ids(s1.id) == [s.id]
    assert store.list_planned_story_ids(s2.id) == []
