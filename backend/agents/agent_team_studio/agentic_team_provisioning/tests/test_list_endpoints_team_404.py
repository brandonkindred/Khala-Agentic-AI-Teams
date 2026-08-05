"""Tests that list endpoints 404 for a non-existent team.

``list_test_chat_sessions``, ``get_agent_quality_scores``, and
``list_pipeline_runs`` previously queried by ``team_id`` without checking the
team exists, returning an empty list instead of 404 for an unknown team. This
was inconsistent with ``create_test_chat_session`` and ``start_pipeline_run``,
which both 404 up front.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_team_studio.agentic_team_provisioning.assistant.store import AgenticTeamStore
from agent_team_studio.agentic_team_provisioning.tests._fake_postgres import install_fake_postgres


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # Fake Postgres must be installed before the API handlers touch the store.
    install_fake_postgres(monkeypatch)
    from agent_team_studio.agentic_team_provisioning.api import main

    return TestClient(main.app)


def _seed_team() -> str:
    store = AgenticTeamStore()
    team = store.create_team(name="Support", description="")
    return team.team_id


@pytest.mark.parametrize(
    "path_suffix, store_method",
    [
        ("/test-chat/sessions", "list_chat_sessions"),
        ("/test-chat/quality-scores", "get_agent_quality_scores"),
        ("/test-pipeline/runs", "list_pipeline_runs"),
    ],
)
def test_list_endpoint_unknown_team_404_and_skips_store(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, path_suffix: str, store_method: str
):
    from agent_team_studio.agentic_team_provisioning.api import main

    def _fail_if_called(*args, **kwargs):
        raise AssertionError(f"{store_method} must not be queried for a non-existent team")

    monkeypatch.setattr(main._test_store, store_method, _fail_if_called)

    resp = client.get(f"/teams/does-not-exist{path_suffix}")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Team not found"


def test_list_pipeline_runs_known_team_returns_empty_list(client: TestClient):
    team_id = _seed_team()
    resp = client.get(f"/teams/{team_id}/test-pipeline/runs")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.parametrize(
    "path_suffix, store_method",
    [
        ("/test-chat/sessions", "list_chat_sessions"),
        ("/test-chat/quality-scores", "get_agent_quality_scores"),
    ],
)
def test_list_endpoint_known_team_reaches_store(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, path_suffix: str, store_method: str
):
    """Once the team exists, the guard passes through and the store is queried."""
    from agent_team_studio.agentic_team_provisioning.api import main

    calls: list[str] = []

    def _record_and_return_empty(team_id: str, *args, **kwargs):
        calls.append(team_id)
        return []

    monkeypatch.setattr(main._test_store, store_method, _record_and_return_empty)

    team_id = _seed_team()
    resp = client.get(f"/teams/{team_id}{path_suffix}")
    assert resp.status_code == 200
    assert resp.json() == []
    assert calls == [team_id]
