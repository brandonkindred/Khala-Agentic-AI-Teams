"""create_team links the new agentic team to the default user profile."""

from __future__ import annotations

import pytest

import agent_team_studio.agentic_team_provisioning.assistant.store as store_mod
from agent_team_studio.agentic_team_provisioning.assistant.store import AgenticTeamStore
from agent_team_studio.agentic_team_provisioning.tests._fake_postgres import install_fake_postgres
from user_profile import ArtifactType


@pytest.fixture
def fake_pg(monkeypatch: pytest.MonkeyPatch) -> dict:
    return install_fake_postgres(monkeypatch)


def test_create_team_records_profile_association(
    fake_pg: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list = []
    monkeypatch.setattr(store_mod, "record_association_safe", lambda *a, **k: calls.append((a, k)))

    store = AgenticTeamStore()
    team = store.create_team("My Team")

    assert calls == [
        (
            (ArtifactType.AGENTIC_TEAM, "agentic_team_provisioning", team.team_id),
            {"label": "My Team"},
        )
    ]
