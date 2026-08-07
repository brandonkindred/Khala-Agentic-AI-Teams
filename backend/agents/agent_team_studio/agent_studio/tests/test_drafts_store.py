"""Unit tests for the in-memory Agent Studio drafts store."""

from __future__ import annotations

import time

import pytest

from agent_team_studio.agent_studio.drafts_store import AgentStudioDraftStore


@pytest.fixture()
def store() -> AgentStudioDraftStore:
    return AgentStudioDraftStore()


def test_create_get_round_trip(store: AgentStudioDraftStore) -> None:
    created = store.create("u1", name="Alpha", payload={"teamId": "t1"})
    loaded = store.get("u1", created.draft_id)
    assert loaded is not None
    assert loaded.draft_id == created.draft_id
    assert loaded.name == "Alpha"
    assert loaded.payload == {"teamId": "t1"}
    assert loaded.created_at == created.created_at
    assert loaded.updated_at == created.updated_at


def test_create_defaults_name_and_empty_payload(store: AgentStudioDraftStore) -> None:
    created = store.create("u1")
    assert created.name  # non-empty timestamp default
    assert created.payload == {}


def test_update_patches_owned_draft(store: AgentStudioDraftStore) -> None:
    created = store.create("u1", name="Old", payload={"a": 1})
    time.sleep(0.01)  # ensure updated_at can move forward on fast clocks
    updated = store.update("u1", created.draft_id, name="New", payload={"a": 2})
    assert updated is not None
    assert updated.name == "New"
    assert updated.payload == {"a": 2}
    assert updated.updated_at >= created.updated_at


def test_update_missing_returns_none(store: AgentStudioDraftStore) -> None:
    assert store.update("u1", "missing", name="x") is None


def test_tenancy_isolation(store: AgentStudioDraftStore) -> None:
    created = store.create("alice", name="Secret", payload={"x": 1})
    assert store.get("bob", created.draft_id) is None
    assert store.update("bob", created.draft_id, name="Hijack") is None
    assert store.rename("bob", created.draft_id, "Hijack") is None
    assert store.delete("bob", created.draft_id) is False
    assert store.list_summaries("bob") == []
    # Alice still owns it unchanged
    assert store.get("alice", created.draft_id) is not None
    assert store.get("alice", created.draft_id).name == "Secret"


def test_rename_and_delete(store: AgentStudioDraftStore) -> None:
    created = store.create("u1", name="Old")
    renamed = store.rename("u1", created.draft_id, "Renamed")
    assert renamed is not None
    assert renamed.name == "Renamed"
    assert store.delete("u1", created.draft_id) is True
    assert store.get("u1", created.draft_id) is None
    assert store.delete("u1", created.draft_id) is False


def test_list_summaries_order_and_pagination(store: AgentStudioDraftStore) -> None:
    ids: list[str] = []
    for i in range(3):
        time.sleep(0.01)
        ids.append(store.create("u1", name=f"d{i}").draft_id)
    # Most recent first
    summaries = store.list_summaries("u1", limit=50, offset=0)
    assert [s.draft_id for s in summaries] == list(reversed(ids))
    page = store.list_summaries("u1", limit=1, offset=1)
    assert len(page) == 1
    assert page[0].draft_id == ids[1]


def test_list_summaries_clamps_limit_and_offset(store: AgentStudioDraftStore) -> None:
    for i in range(5):
        store.create("u1", name=f"n{i}")
    assert len(store.list_summaries("u1", limit=0)) == 1  # clamped to 1
    assert len(store.list_summaries("u1", limit=1000)) == 5  # clamped to 100, but only 5 exist
    assert store.list_summaries("u1", limit=50, offset=-5)  # negative offset → 0


def test_preconditions(store: AgentStudioDraftStore) -> None:
    with pytest.raises(ValueError):
        store.create("")
    with pytest.raises(ValueError):
        store.create("u1", name="")
    with pytest.raises(ValueError):
        store.create("u1", payload=["not", "a", "dict"])  # type: ignore[arg-type]
    created = store.create("u1", name="ok")
    with pytest.raises(ValueError):
        store.rename("u1", created.draft_id, "")
