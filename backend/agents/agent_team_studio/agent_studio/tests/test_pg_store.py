"""Live-Postgres tests for the durable Agent Studio conversation store.

Skipped when ``POSTGRES_HOST`` is unset. Exercises the CRUD parity with the
in-memory store plus the row-lock turn serialization (P3/P4).
"""

from __future__ import annotations

import threading
import time

import pytest

from agent_team_studio.agent_studio.models import AgentDefinition
from shared.postgres import is_postgres_enabled

pytestmark = pytest.mark.skipif(
    not is_postgres_enabled(), reason="POSTGRES_HOST not set; skipping live-Postgres store tests"
)


@pytest.fixture()
def store():
    from agent_team_studio.agent_studio.pg_store import PostgresAgentStudioConversationStore
    from agent_team_studio.agent_studio.postgres import SCHEMA
    from shared.postgres import register_team_schemas
    from shared.postgres.testing import truncate_team_tables

    register_team_schemas(SCHEMA)
    truncate_team_tables(SCHEMA)
    return PostgresAgentStudioConversationStore()


def test_create_get_round_trip(store) -> None:
    cid = store.create("refine", "blogging.planner", AgentDefinition(name="x", role="r"))
    record = store.get(cid)
    assert record is not None
    assert record.conversation_id == cid
    assert record.mode == "refine"
    assert record.source_agent_id == "blogging.planner"
    assert record.definition.name == "x"
    assert record.messages == []


def test_get_unknown_returns_none(store) -> None:
    assert store.get("nope") is None


def test_append_message_accumulates_and_orders(store) -> None:
    cid = store.create("new", None, AgentDefinition(name="x", role="r"))
    store.append_message(cid, "user", "hi")
    store.append_message(cid, "assistant", "hello")
    msgs = store.get(cid).messages
    assert [(m.role, m.content) for m in msgs] == [("user", "hi"), ("assistant", "hello")]


def test_append_message_unknown_raises(store) -> None:
    with pytest.raises(LookupError):
        store.append_message("nope", "user", "hi")


def test_discard_removes_conversation_and_messages(store) -> None:
    cid = store.create("new", None, AgentDefinition(name="x", role="r"))
    store.append_message(cid, "user", "hi")
    store.discard(cid)
    assert store.get(cid) is None
    assert len(store) == 0


def test_discard_unknown_is_noop(store) -> None:
    store.discard("nope")  # must not raise


def test_len_counts_conversations(store) -> None:
    store.create("new", None, AgentDefinition(name="a", role="r"))
    store.create("new", None, AgentDefinition(name="b", role="r"))
    assert len(store) == 2


def test_turn_applies_messages_and_definition(store) -> None:
    cid = store.create("new", None, AgentDefinition(name="x", role="r"))
    with store.turn(cid) as t:
        assert t.history == []
        t.append_message("user", "hi")
        t.append_message("assistant", "hello")
        updated = t.definition.model_copy()
        updated.name = "Renamed"
        t.set_definition(updated)
    record = store.get(cid)
    assert [m.content for m in record.messages] == ["hi", "hello"]
    assert record.definition.name == "Renamed"


def test_turn_unknown_conversation_raises(store) -> None:
    with pytest.raises(LookupError):
        with store.turn("nope"):
            pass


def test_turn_rolls_back_on_exception(store) -> None:
    cid = store.create("new", None, AgentDefinition(name="x", role="r"))
    with pytest.raises(RuntimeError):
        with store.turn(cid) as t:
            t.append_message("user", "partial")
            raise RuntimeError("boom")
    # The whole turn transaction rolled back — no partial message persisted.
    assert store.get(cid).messages == []


def test_turn_row_lock_serializes_concurrent_turns(store) -> None:
    # Two threads run overlapping turns that read→+1→write a counter in the
    # definition. The SELECT ... FOR UPDATE row lock forces the second to block
    # until the first commits, so no update is lost.
    cid = store.create("new", None, AgentDefinition(name="x", role="r", description="0"))
    n = 5
    barrier = threading.Barrier(n)

    def do_turn() -> None:
        barrier.wait()
        with store.turn(cid) as t:
            current = int(t.definition.description or "0")
            time.sleep(0.02)
            updated = t.definition.model_copy()
            updated.description = str(current + 1)
            t.set_definition(updated)

    threads = [threading.Thread(target=do_turn) for _ in range(n)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert store.get(cid).definition.description == str(n)
