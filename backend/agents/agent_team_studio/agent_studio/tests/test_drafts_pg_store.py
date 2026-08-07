"""Live-Postgres tests for the durable Agent Studio drafts store.

Skipped when ``POSTGRES_HOST`` is unset.
"""

from __future__ import annotations

import time

import pytest

from shared.postgres import is_postgres_enabled

pytestmark = pytest.mark.skipif(
    not is_postgres_enabled(), reason="POSTGRES_HOST not set; skipping live-Postgres draft store tests"
)


@pytest.fixture()
def store():
    from agent_team_studio.agent_studio.drafts_pg_store import PostgresAgentStudioDraftStore
    from agent_team_studio.agent_studio.postgres import SCHEMA
    from shared.postgres import register_team_schemas
    from shared.postgres.testing import truncate_team_tables

    register_team_schemas(SCHEMA)
    truncate_team_tables(SCHEMA)
    return PostgresAgentStudioDraftStore()


def test_create_get_round_trip(store) -> None:
    created = store.create("u1", name="Alpha", payload={"teamId": "t1"})
    loaded = store.get("u1", created.draft_id)
    assert loaded is not None
    assert loaded.name == "Alpha"
    assert loaded.payload == {"teamId": "t1"}


def test_tenancy_isolation(store) -> None:
    created = store.create("alice", name="Secret", payload={"x": 1})
    assert store.get("bob", created.draft_id) is None
    assert store.update("bob", created.draft_id, name="Hijack") is None
    assert store.rename("bob", created.draft_id, "Hijack") is None
    assert store.delete("bob", created.draft_id) is False
    assert store.list_summaries("bob") == []
    assert store.get("alice", created.draft_id).name == "Secret"


def test_list_summaries_order_and_pagination(store) -> None:
    ids: list[str] = []
    for i in range(3):
        time.sleep(0.01)
        ids.append(store.create("u1", name=f"d{i}").draft_id)
    summaries = store.list_summaries("u1", limit=50, offset=0)
    assert [s.draft_id for s in summaries] == list(reversed(ids))
    page = store.list_summaries("u1", limit=1, offset=1)
    assert len(page) == 1
    assert page[0].draft_id == ids[1]


def test_update_rename_delete(store) -> None:
    created = store.create("u1", name="Old", payload={"a": 1})
    updated = store.update("u1", created.draft_id, payload={"a": 2})
    assert updated is not None and updated.payload == {"a": 2}
    renamed = store.rename("u1", created.draft_id, "Renamed")
    assert renamed is not None and renamed.name == "Renamed"
    assert store.delete("u1", created.draft_id) is True
    assert store.get("u1", created.draft_id) is None


def test_list_clamps_limit(store) -> None:
    for i in range(3):
        store.create("u1", name=f"n{i}")
    assert len(store.list_summaries("u1", limit=0)) == 1
    assert len(store.list_summaries("u1", limit=1000)) == 3
