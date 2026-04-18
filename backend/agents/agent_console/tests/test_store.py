"""Integration tests for :class:`AgentConsoleStore` against a live Postgres.

Skipped automatically when ``POSTGRES_HOST`` is unset (matches the pattern
used by ``shared_postgres`` tests).
"""

from __future__ import annotations

import pytest

from agent_console.models import RunCreate
from agent_console.postgres import SCHEMA
from agent_console.store import (
    AgentConsoleStore,
    SavedInputNameConflict,
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
    # Clear singleton cache so each test gets a fresh-looking store.
    get_store.cache_clear()


def _store() -> AgentConsoleStore:
    return get_store()


# ------------------------------------------------------------------
# Saved inputs
# ------------------------------------------------------------------


def test_create_and_list_saved_inputs() -> None:
    store = _store()
    a = store.create_saved_input(
        agent_id="blogging.planner",
        name="hello",
        input_data={"brief": "hi"},
        author="tester",
        description="seed",
    )
    b = store.create_saved_input(
        agent_id="blogging.planner",
        name="world",
        input_data={"brief": "hey"},
        author="tester",
        description=None,
    )
    rows = store.list_saved_inputs("blogging.planner")
    assert [r.id for r in rows] == [b.id, a.id]  # newest-first
    assert rows[0].input_data == {"brief": "hey"}


def test_duplicate_saved_input_name_raises_conflict() -> None:
    store = _store()
    store.create_saved_input(
        agent_id="blogging.planner",
        name="dup",
        input_data={"brief": "a"},
        author="tester",
        description=None,
    )
    with pytest.raises(SavedInputNameConflict):
        store.create_saved_input(
            agent_id="blogging.planner",
            name="dup",
            input_data={"brief": "b"},
            author="tester",
            description=None,
        )


def test_update_saved_input_body_and_description() -> None:
    store = _store()
    saved = store.create_saved_input(
        agent_id="blogging.planner",
        name="keep",
        input_data={"brief": "a"},
        author="tester",
        description="first",
    )
    updated = store.update_saved_input(saved.id, input_data={"brief": "b"}, description="second")
    assert updated is not None
    assert updated.input_data == {"brief": "b"}
    assert updated.description == "second"


def test_delete_saved_input() -> None:
    store = _store()
    saved = store.create_saved_input(
        agent_id="blogging.planner",
        name="temp",
        input_data={"brief": ""},
        author="tester",
        description=None,
    )
    assert store.delete_saved_input(saved.id) is True
    assert store.delete_saved_input(saved.id) is False


# ------------------------------------------------------------------
# Runs
# ------------------------------------------------------------------


def _make_run(agent_id: str, idx: int) -> RunCreate:
    return RunCreate(
        agent_id=agent_id,
        team="blogging",
        saved_input_id=None,
        input_data={"idx": idx},
        output_data={"idx": idx, "out": True},
        error=None,
        status="ok",
        duration_ms=100 + idx,
        trace_id=f"trace-{idx}",
        logs_tail=[f"log {idx}"],
        author="tester",
        sandbox_url="http://localhost:8200",
    )


def test_record_and_list_runs_newest_first() -> None:
    store = _store()
    for i in range(3):
        store.record_run(_make_run("blogging.planner", i))
    rows = store.list_runs("blogging.planner")
    assert [r.trace_id for r in rows] == ["trace-2", "trace-1", "trace-0"]


def test_list_runs_cursor_pagination() -> None:
    store = _store()
    for i in range(5):
        store.record_run(_make_run("blogging.planner", i))
    page_one = store.list_runs("blogging.planner", limit=2)
    assert len(page_one) == 2
    page_two = store.list_runs("blogging.planner", limit=2, cursor=page_one[-1].created_at)
    assert len(page_two) == 2
    assert page_one[-1].trace_id != page_two[0].trace_id
    seen_ids = {r.id for r in page_one} | {r.id for r in page_two}
    assert len(seen_ids) == 4


def test_get_run_returns_full_payload() -> None:
    store = _store()
    stored = store.record_run(_make_run("blogging.planner", 7))
    loaded = store.get_run(stored.id)
    assert loaded is not None
    assert loaded.output_data == {"idx": 7, "out": True}
    assert loaded.logs_tail == ["log 7"]


def test_delete_run() -> None:
    store = _store()
    stored = store.record_run(_make_run("blogging.planner", 1))
    assert store.delete_run(stored.id) is True
    assert store.get_run(stored.id) is None
