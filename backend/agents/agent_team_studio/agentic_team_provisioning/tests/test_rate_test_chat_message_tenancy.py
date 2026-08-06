"""Tests that rate_test_chat_message enforces the team_id path tenancy boundary.

Previously the endpoint called ``_test_store.update_message_rating(message_id,
rating)`` without any team scoping, so a caller who knew a message id from a
different team's chat session could rate it. The fix threads ``team_id``
through to the store call, which now only updates (and reports success for) a
message whose owning session belongs to that team.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_team_studio.agentic_team_provisioning.tests._fake_postgres import install_fake_postgres


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # Fake Postgres must be installed before the API handlers touch the store.
    install_fake_postgres(monkeypatch)
    from agent_team_studio.agentic_team_provisioning.api import main

    return TestClient(main.app)


class _FakeRatingStore:
    """Mimics the team-scoped SQL: a rating only applies when the message's
    recorded owning team matches the team_id passed in."""

    def __init__(self, message_owners: dict[str, str]):
        self._message_owners = message_owners
        self.ratings: dict[str, str] = {}
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, team_id: str, message_id: str, rating: str) -> bool:
        self.calls.append((team_id, message_id, rating))
        if self._message_owners.get(message_id) != team_id:
            return False
        self.ratings[message_id] = rating
        return True


def test_rate_message_cross_team_returns_404_and_does_not_mutate(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    from agent_team_studio.agentic_team_provisioning.api import main

    message_id = "msg-1"
    fake_store = _FakeRatingStore({message_id: "team-owner"})
    monkeypatch.setattr(main._test_store, "update_message_rating", fake_store)

    resp = client.put(
        f"/teams/team-attacker/test-chat/messages/{message_id}/rating",
        json={"rating": "thumbs_up"},
    )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Message not found"
    # The store was asked to scope by the (wrong) path team_id, and rejected it.
    assert fake_store.calls == [("team-attacker", message_id, "thumbs_up")]
    assert message_id not in fake_store.ratings


def test_rate_message_same_team_succeeds(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    from agent_team_studio.agentic_team_provisioning.api import main

    message_id = "msg-2"
    fake_store = _FakeRatingStore({message_id: "team-owner"})
    monkeypatch.setattr(main._test_store, "update_message_rating", fake_store)

    resp = client.put(
        f"/teams/team-owner/test-chat/messages/{message_id}/rating",
        json={"rating": "thumbs_down"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"message_id": message_id, "rating": "thumbs_down"}
    assert fake_store.ratings[message_id] == "thumbs_down"
