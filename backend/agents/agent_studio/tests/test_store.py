"""Unit tests for :mod:`agent_studio.store`."""

from __future__ import annotations

import pytest

from agent_studio.models import AgentDefinition
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
    with pytest.raises(AssertionError):
        AgentStudioConversationStore(max_conversations=0)


def test_default_max_conversations_from_env(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_STUDIO_MAX_CONVERSATIONS", "7")
    assert _default_max_conversations() == 7


def test_default_max_conversations_garbage_falls_back(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_STUDIO_MAX_CONVERSATIONS", "not-a-number")
    assert _default_max_conversations() == 1000


def test_default_max_conversations_non_positive_falls_back(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_STUDIO_MAX_CONVERSATIONS", "-5")
    assert _default_max_conversations() == 1000
