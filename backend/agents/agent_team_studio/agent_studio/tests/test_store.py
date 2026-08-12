"""Unit tests for :mod:`agent_team_studio.agent_studio.store`."""

from __future__ import annotations

import threading
import time

import pytest

from agent_team_studio.agent_studio.models import AgentDefinition, ConversationMessage
from agent_team_studio.agent_studio.store import (
    AgentStudioConversationStore,
    _default_max_conversations,
)


def test_create_returns_fresh_record() -> None:
    store = AgentStudioConversationStore()
    cid = store.create("new", None, AgentDefinition())
    record = store.get(cid)
    assert record is not None
    assert record.conversation_id == cid
    assert record.mode == "new"
    assert record.source_agent_id is None
    assert record.messages == []


def test_get_unknown_returns_none() -> None:
    assert AgentStudioConversationStore().get("nope") is None


def test_ids_are_unique() -> None:
    store = AgentStudioConversationStore()
    a = store.create("new", None, AgentDefinition())
    b = store.create("new", None, AgentDefinition())
    assert a != b


def test_append_message_accumulates() -> None:
    store = AgentStudioConversationStore()
    cid = store.create("new", None, AgentDefinition())
    store.append_message(cid, "user", "hi")
    store.append_message(cid, "assistant", "hello")
    msgs = store.get(cid).messages
    assert [(m.role, m.content) for m in msgs] == [("user", "hi"), ("assistant", "hello")]


def test_set_definition_replaces() -> None:
    store = AgentStudioConversationStore()
    cid = store.create("refine", "src", AgentDefinition(name="old"))
    store.set_definition(cid, AgentDefinition(name="new", role="r"))
    assert store.get(cid).definition.name == "new"


def test_bounded_store_evicts_oldest() -> None:
    store = AgentStudioConversationStore(max_conversations=2)
    a = store.create("new", None, AgentDefinition())
    b = store.create("new", None, AgentDefinition())
    c = store.create("new", None, AgentDefinition())
    # Oldest (a) evicted; b and c remain. Cap holds.
    assert store.get(a) is None
    assert store.get(b) is not None
    assert store.get(c) is not None


def test_invalid_max_conversations_rejected() -> None:
    # Explicit raise (not assert) so it survives `python -O`.
    with pytest.raises(ValueError):
        AgentStudioConversationStore(max_conversations=0)


def test_get_returns_snapshot_not_internal_record() -> None:
    store = AgentStudioConversationStore()
    cid = store.create("new", None, AgentDefinition())
    store.append_message(cid, "user", "hi")
    snap = store.get(cid)
    # Mutating the returned snapshot must not touch the store's internal state.
    snap.messages.append(ConversationMessage(role="user", content="injected"))
    assert len(store.get(cid).messages) == 1


def test_get_snapshot_definition_is_independent() -> None:
    store = AgentStudioConversationStore()
    cid = store.create("new", None, AgentDefinition(name="orig"))
    snap = store.get(cid)
    snap.definition.name = "mutated"  # deep-copied → must not affect the store
    assert store.get(cid).definition.name == "orig"


def test_lru_eviction_keeps_recently_used() -> None:
    # Touching a conversation marks it most-recently-used, so the cap evicts the
    # least-recently-used rather than a still-active conversation.
    store = AgentStudioConversationStore(max_conversations=2)
    a = store.create("new", None, AgentDefinition())
    b = store.create("new", None, AgentDefinition())
    store.get(a)  # a becomes most-recently-used
    c = store.create("new", None, AgentDefinition())
    assert store.get(b) is None  # b was least-recently-used → evicted
    assert store.get(a) is not None
    assert store.get(c) is not None


def test_default_max_conversations_from_env(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_STUDIO_MAX_CONVERSATIONS", "7")
    assert _default_max_conversations() == 7


def test_default_max_conversations_garbage_falls_back(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_STUDIO_MAX_CONVERSATIONS", "not-a-number")
    assert _default_max_conversations() == 1000


def test_default_max_conversations_non_positive_falls_back(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_STUDIO_MAX_CONVERSATIONS", "-5")
    assert _default_max_conversations() == 1000


def test_append_message_unknown_id_raises_lookup_error() -> None:
    with pytest.raises(LookupError):
        AgentStudioConversationStore().append_message("nope", "user", "hi")


def test_set_definition_unknown_id_raises_lookup_error() -> None:
    with pytest.raises(LookupError):
        AgentStudioConversationStore().set_definition("nope", AgentDefinition())


def test_discard_removes_conversation() -> None:
    store = AgentStudioConversationStore()
    cid = store.create("new", None, AgentDefinition())
    store.discard(cid)
    assert store.get(cid) is None
    assert len(store) == 0


def test_discard_unknown_id_is_noop() -> None:
    # Unlike append/set_definition, discard must not raise on an unknown id, so
    # cleanup can't mask the original failure with a second exception.
    store = AgentStudioConversationStore()
    store.discard("nope")  # no exception
    assert len(store) == 0


def test_discard_drops_the_turn_lock_entry() -> None:
    # The turn-lock table (assistant_kernel.InMemoryTurnLocks) is keyed
    # independently of ConversationRecord's lifetime, so discard() must drop its
    # entry too — otherwise a removed conversation's lock lingers forever.
    store = AgentStudioConversationStore()
    cid = store.create("new", None, AgentDefinition())
    with store.turn(cid):
        pass
    assert len(store._turn_locks) == 1
    store.discard(cid)
    assert len(store._turn_locks) == 0


def test_eviction_drops_the_turn_lock_entry() -> None:
    # Same leak-prevention guarantee as discard(), but for LRU eviction: an
    # evicted conversation's lock-table entry must not linger either.
    store = AgentStudioConversationStore(max_conversations=1)
    a = store.create("new", None, AgentDefinition())
    with store.turn(a):
        pass
    assert len(store._turn_locks) == 1
    store.create("new", None, AgentDefinition())  # evicts `a`
    assert len(store._turn_locks) == 0


def test_concurrent_creates_are_thread_safe() -> None:
    # Many threads hammering create() must not corrupt the OrderedDict or
    # violate the cap; every returned id is unique.
    cap = 50
    store = AgentStudioConversationStore(max_conversations=cap)
    ids: list[str] = []
    ids_lock = threading.Lock()
    barrier = threading.Barrier(20)

    def worker() -> None:
        barrier.wait()  # maximize contention
        for _ in range(25):
            cid = store.create("new", None, AgentDefinition())
            with ids_lock:
                ids.append(cid)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(ids) == 20 * 25
    assert len(set(ids)) == len(ids)  # no duplicate / lost ids
    assert len(store) == cap  # cap held under concurrency (public __len__)


# ---------------------------------------------------------------------------
# turn() — per-conversation turn serialization (P4)
# ---------------------------------------------------------------------------


def test_turn_applies_messages_and_definition() -> None:
    store = AgentStudioConversationStore()
    cid = store.create("new", None, AgentDefinition(name="x", role="r"))
    with store.turn(cid) as t:
        assert t.history == []
        assert t.draft.name == "x"
        t.append_message("user", "hi")
        t.append_message("assistant", "hello")
        updated = t.draft.model_copy()
        updated.name = "Renamed"
        t.set_draft(updated)
    record = store.get(cid)
    assert [m.content for m in record.messages] == ["hi", "hello"]
    assert record.definition.name == "Renamed"


def test_turn_unknown_conversation_raises() -> None:
    store = AgentStudioConversationStore()
    with pytest.raises(LookupError):
        with store.turn("nope"):
            pass


def test_turn_history_snapshots_prior_messages() -> None:
    store = AgentStudioConversationStore()
    cid = store.create("new", None, AgentDefinition(name="x", role="r"))
    store.append_message(cid, "assistant", "greeting")
    with store.turn(cid) as t:
        assert t.history == [("assistant", "greeting")]


def test_turn_serializes_concurrent_turns_no_lost_update() -> None:
    # Each turn reads a counter from the definition, waits (widening the race
    # window), then writes counter+1. Without serialization concurrent turns would
    # read the same base and lose updates; the per-conversation lock guarantees the
    # final value equals the number of turns.
    store = AgentStudioConversationStore()
    cid = store.create("new", None, AgentDefinition(name="x", role="r", description="0"))
    n = 12
    barrier = threading.Barrier(n)

    def do_turn() -> None:
        barrier.wait()
        with store.turn(cid) as t:
            current = int(t.draft.description or "0")
            time.sleep(0.003)
            updated = t.draft.model_copy()
            updated.description = str(current + 1)
            t.set_draft(updated)

    threads = [threading.Thread(target=do_turn) for _ in range(n)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert store.get(cid).definition.description == str(n)


def test_turn_rolls_back_nothing_on_exception() -> None:
    # If the body raises before any write (assistant-first ordering), the
    # conversation is unchanged and the lock is released for the next turn.
    store = AgentStudioConversationStore()
    cid = store.create("new", None, AgentDefinition(name="x", role="r"))
    with pytest.raises(RuntimeError):
        with store.turn(cid):
            raise RuntimeError("llm blew up")
    assert store.get(cid).messages == []
    # Lock released: a subsequent turn proceeds.
    with store.turn(cid) as t:
        t.append_message("user", "again")
    assert len(store.get(cid).messages) == 1


def test_turn_rolls_back_a_partial_write_on_later_exception() -> None:
    # Unlike a failure before any write, an exception AFTER a message was already
    # appended must still leave the conversation exactly as it was pre-turn (no
    # partially-applied state) — each in-memory write applies immediately, so the
    # store must explicitly restore the snapshot rather than relying on a
    # transaction.
    store = AgentStudioConversationStore()
    cid = store.create("new", None, AgentDefinition(name="x", role="r"))
    with pytest.raises(RuntimeError):
        with store.turn(cid) as t:
            t.append_message("user", "hi")
            updated = t.draft.model_copy()
            updated.name = "should-not-stick"
            t.set_draft(updated)
            raise RuntimeError("failed after partial writes")
    record = store.get(cid)
    assert record.messages == []
    assert record.definition.name == "x"
    # Lock released: a subsequent turn proceeds normally.
    with store.turn(cid) as t:
        t.append_message("user", "again")
    assert len(store.get(cid).messages) == 1
