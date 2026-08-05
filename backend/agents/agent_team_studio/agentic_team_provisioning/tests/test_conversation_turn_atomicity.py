"""A conversation turn is persisted only after the full turn succeeds.

Covers the fix for the pre-existing bug where ``create_conversation`` and
``send_message`` appended the user and assistant messages *before* calling
``_save_agents_from_llm`` (which registers the LLM's roster into the agent
registry). A registry failure raised after the append left a half-saved turn
in the conversation history — a client retry would then duplicate it. Both
routes now persist the turn's messages only after the LLM call and the
roster/process saves have succeeded.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_team_studio.agentic_team_provisioning.assistant.store import AgenticTeamStore
from agent_team_studio.agentic_team_provisioning.models import ProcessDefinition
from agent_team_studio.agentic_team_provisioning.tests._fake_postgres import install_fake_postgres

_REPLY = ("Sure, let's design that.", None, ["What's next?"], None)


@pytest.fixture
def fake_pg(monkeypatch: pytest.MonkeyPatch) -> dict:
    db = install_fake_postgres(monkeypatch)
    import agent_team_studio.agentic_team_provisioning.assistant.store as store_mod

    monkeypatch.setattr(store_mod, "record_association_safe", lambda *a, **k: None)
    monkeypatch.setattr(store_mod, "remove_association_safe", lambda *a, **k: None)
    return db


@pytest.fixture
def client(fake_pg: dict) -> TestClient:
    from agent_team_studio.agentic_team_provisioning.api.main import app

    return TestClient(app)


def _new_team() -> str:
    return AgenticTeamStore().create_team(name="Growth Pod", description="").team_id


def test_send_message_leaves_no_partial_turn_when_registry_fails(
    fake_pg: dict, monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """A registry failure during send_message must not leave a half-saved turn.

    Preconditions: an existing conversation with no messages; ``_agent.respond``
        returns an ``agents`` block, and ``register_team_manifests`` (invoked
        via ``_save_agents_from_llm``) is patched to raise.
    Postconditions: the response is a 503 and the conversation's
        message history is unchanged (neither the user nor the assistant
        message from the failed turn was appended).
    """
    import agent_team_studio.agentic_team_provisioning.api.main as main_mod

    team_id = _new_team()
    conversation_id = AgenticTeamStore().create_conversation(team_id=team_id)

    monkeypatch.setattr(
        main_mod._agent,
        "respond",
        lambda **kwargs: (
            "Sure, let's design that.",
            None,
            ["What's next?"],
            [{"agent_name": "Planner", "role": "Plans things"}],
        ),
    )

    def _boom(*args, **kwargs):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(main_mod, "register_team_manifests", _boom)

    resp = client.post(f"/conversations/{conversation_id}/messages", json={"message": "hi"})

    assert resp.status_code == 503
    assert AgenticTeamStore().get_messages(conversation_id) == []


def test_send_message_persists_turn_on_success(
    fake_pg: dict, monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """Happy-path regression: a successful turn appends both messages in order.

    Preconditions: an existing conversation with no messages; ``_agent.respond``
        is patched to return a reply with no roster/process update.
    Postconditions: the response is 200 and the conversation history holds
        exactly the user message followed by the assistant reply.
    """
    import agent_team_studio.agentic_team_provisioning.api.main as main_mod

    team_id = _new_team()
    conversation_id = AgenticTeamStore().create_conversation(team_id=team_id)

    monkeypatch.setattr(main_mod._agent, "respond", lambda **kwargs: _REPLY)

    resp = client.post(f"/conversations/{conversation_id}/messages", json={"message": "hi"})

    assert resp.status_code == 200
    messages = AgenticTeamStore().get_messages(conversation_id)
    assert [(m.role, m.content) for m in messages] == [
        ("user", "hi"),
        ("assistant", "Sure, let's design that."),
    ]


def test_send_message_propagates_non_retryable_errors_instead_of_503(
    fake_pg: dict, monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """A non-retryable data/programming error must not be disguised as a 503.

    Preconditions: ``_agent.respond`` returns an ``agents`` block, and
        ``register_team_manifests`` is patched to raise ``ValueError`` (e.g.
        malformed LLM output) rather than a transient failure.
    Postconditions: the ``ValueError`` propagates as itself — not swallowed
        into an ``HTTPException(503)`` implying a retry could succeed — and
        no messages were persisted.
    """
    import agent_team_studio.agentic_team_provisioning.api.main as main_mod

    team_id = _new_team()
    conversation_id = AgenticTeamStore().create_conversation(team_id=team_id)

    monkeypatch.setattr(
        main_mod._agent,
        "respond",
        lambda **kwargs: (
            "Sure, let's design that.",
            None,
            ["What's next?"],
            [{"agent_name": "Planner", "role": "Plans things"}],
        ),
    )

    def _boom(*args, **kwargs):
        raise ValueError("malformed agents_data")

    monkeypatch.setattr(main_mod, "register_team_manifests", _boom)

    with pytest.raises(ValueError, match="malformed agents_data"):
        client.post(f"/conversations/{conversation_id}/messages", json={"message": "hi"})

    assert AgenticTeamStore().get_messages(conversation_id) == []


def test_send_message_persists_turn_when_background_provisioning_fails(
    fake_pg: dict, monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """A background-provisioning-scheduling failure must not discard the turn.

    Preconditions: an existing conversation; ``_agent.respond`` returns a
        ``process`` update, and ``schedule_provision_step_agents`` (invoked via
        ``_after_process_saved``, after the process is already saved and
        linked) is patched to raise.
    Postconditions: the response is 200 — the roster/process save already
        committed and scheduling provisioning is best-effort, so its failure
        is logged and swallowed rather than discarding the already-successful
        turn — and both messages are persisted.
    """
    import agent_team_studio.agentic_team_provisioning.api.main as main_mod

    team_id = _new_team()
    conversation_id = AgenticTeamStore().create_conversation(team_id=team_id)
    process = ProcessDefinition(process_id="p1")

    monkeypatch.setattr(
        main_mod._agent,
        "respond",
        lambda **kwargs: ("Sure, let's design that.", process, ["What's next?"], None),
    )

    def _boom(*args, **kwargs):
        raise RuntimeError("provisioning service unavailable")

    monkeypatch.setattr(main_mod, "schedule_provision_step_agents", _boom)

    resp = client.post(f"/conversations/{conversation_id}/messages", json={"message": "hi"})

    assert resp.status_code == 200
    messages = AgenticTeamStore().get_messages(conversation_id)
    assert [(m.role, m.content) for m in messages] == [
        ("user", "hi"),
        ("assistant", "Sure, let's design that."),
    ]


def test_create_conversation_leaves_no_messages_when_registry_fails(
    fake_pg: dict, monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """A registry failure on the first turn must not leave a half-saved conversation.

    Preconditions: an existing team; ``_agent.respond`` returns an ``agents``
        block, and ``register_team_manifests`` is patched to raise.
    Postconditions: the response is a 503 and the newly created
        conversation has no messages (not even the user's initial message).
    """
    import agent_team_studio.agentic_team_provisioning.api.main as main_mod

    team_id = _new_team()

    monkeypatch.setattr(
        main_mod._agent,
        "respond",
        lambda **kwargs: (
            "Hello!",
            None,
            [],
            [{"agent_name": "Planner", "role": "Plans things"}],
        ),
    )

    def _boom(*args, **kwargs):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(main_mod, "register_team_manifests", _boom)

    resp = client.post("/conversations", json={"team_id": team_id, "initial_message": "hi there"})

    assert resp.status_code == 503
    conversations = [c for c in fake_pg["conversations"].values() if c["team_id"] == team_id]
    assert len(conversations) == 1
    assert AgenticTeamStore().get_messages(conversations[0]["conversation_id"]) == []


def test_create_conversation_persists_turn_on_success(
    fake_pg: dict, monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """Happy-path regression: a successful first turn appends both messages.

    Preconditions: an existing team; ``_agent.respond`` is patched to return
        a reply with no roster/process update.
    Postconditions: the response is 200 and the created conversation holds
        exactly the user's initial message followed by the assistant reply.
    """
    import agent_team_studio.agentic_team_provisioning.api.main as main_mod

    team_id = _new_team()
    monkeypatch.setattr(main_mod._agent, "respond", lambda **kwargs: _REPLY)

    resp = client.post("/conversations", json={"team_id": team_id, "initial_message": "hi there"})

    assert resp.status_code == 200
    body = resp.json()
    assert [(m["role"], m["content"]) for m in body["messages"]] == [
        ("user", "hi there"),
        ("assistant", "Sure, let's design that."),
    ]
