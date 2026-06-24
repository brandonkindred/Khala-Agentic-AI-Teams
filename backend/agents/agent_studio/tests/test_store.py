"""Unit tests for :mod:`agent_studio.store`."""

from __future__ import annotations

from agent_studio.models import AgentDefinition
from agent_studio.store import AgentStudioConversationStore


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
