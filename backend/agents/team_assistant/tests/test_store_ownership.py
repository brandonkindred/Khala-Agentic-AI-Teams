"""Tests for team_key-scoped ownership checks on TeamAssistantConversationStore.

Verifies that mutating operations (``append_message``, ``add_artifact``)
refuse to touch a conversation that belongs to a different ``team_key``.
"""

from __future__ import annotations

import pytest

from team_assistant.store import TeamAssistantConversationStore
from team_assistant.tests._fake_postgres import install_fake_postgres


@pytest.fixture
def fake_pg(monkeypatch: pytest.MonkeyPatch) -> dict:
    return install_fake_postgres(monkeypatch)


def test_add_artifact_refuses_cross_team_conversation(fake_pg: dict) -> None:
    """A store scoped to team B must not persist artifacts onto a team A conversation."""
    store_a = TeamAssistantConversationStore(team_key="team_a")
    store_b = TeamAssistantConversationStore(team_key="team_b")

    cid = store_a.create(conversation_id="conv-1")

    # Team B tries to attach an artifact to team A's conversation.
    art_id = store_b.add_artifact(cid, "advice", "Cross-team leak", {"secret": "nope"})

    # Ownership check fires — sentinel 0 returned, nothing persisted.
    assert art_id == 0
    assert fake_pg["artifacts"] == []

    # Team A still sees an empty artifact list (the leak attempt had no effect).
    assert store_a.get_artifacts(cid) == []


def test_add_artifact_refuses_unknown_conversation(fake_pg: dict) -> None:
    store = TeamAssistantConversationStore(team_key="team_a")
    art_id = store.add_artifact("no-such-cid", "advice", "ghost", {"x": 1})
    assert art_id == 0
    assert fake_pg["artifacts"] == []


def test_add_artifact_persists_for_owning_team(fake_pg: dict) -> None:
    """Owning team can still add artifacts — baseline regression guard."""
    store = TeamAssistantConversationStore(team_key="team_a")
    cid = store.create(conversation_id="conv-1")

    art_id = store.add_artifact(cid, "advice", "Title", {"content": "hello"})
    assert art_id > 0

    arts = store.get_artifacts(cid)
    assert [a.artifact_id for a in arts] == [art_id]
    assert arts[0].payload == {"content": "hello"}


def test_append_message_refuses_cross_team_conversation(fake_pg: dict) -> None:
    """Regression guard: append_message's existing ownership check still works."""
    store_a = TeamAssistantConversationStore(team_key="team_a")
    store_b = TeamAssistantConversationStore(team_key="team_b")

    cid = store_a.create(conversation_id="conv-2")

    assert store_b.append_message(cid, "user", "intruder") is False
    assert fake_pg["messages"] == []
