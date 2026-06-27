"""Unit tests for :mod:`agent_studio.store`."""

from __future__ import annotations

import threading

import pytest

from agent_studio.models import AgentDefinition, ConversationMessage
from agent_studio.store import AgentStudioConversationStore, _default_max_conversations


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
    assert len(store._records) == cap  # cap held under concurrency
