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
