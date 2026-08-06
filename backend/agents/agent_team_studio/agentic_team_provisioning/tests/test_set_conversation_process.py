"""Tests for PUT /conversations/{conversation_id}/process.

Previously the route accepted body: dict and manually pulled process_id out
of it, bypassing FastAPI/Pydantic's automatic validation. It's now a typed
SetConversationProcessRequest with a required process_id field.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_team_studio.agentic_team_provisioning.assistant.store import AgenticTeamStore
from agent_team_studio.agentic_team_provisioning.models import (
    ProcessDefinition,
    ProcessStep,
    ProcessStepAgent,
    StepType,
)
from agent_team_studio.agentic_team_provisioning.tests._fake_postgres import install_fake_postgres


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    install_fake_postgres(monkeypatch)
    from agent_team_studio.agentic_team_provisioning.api import main

    return TestClient(main.app)


def _seed_team_conversation_and_process() -> tuple[str, str, str]:
    store = AgenticTeamStore()
    team = store.create_team(name="Ops", description="")
    conversation_id = store.create_conversation(team_id=team.team_id)
    process = ProcessDefinition(
        process_id="proc-1",
        name="P",
        steps=[
            ProcessStep(
                step_id="s1",
                name="Do work",
                step_type=StepType.ACTION,
                agents=[ProcessStepAgent(agent_name="worker", role="doer")],
            )
        ],
    )
    store.save_process(team.team_id, process)
    return team.team_id, conversation_id, process.process_id


def test_set_conversation_process_success(client: TestClient):
    _, conversation_id, process_id = _seed_team_conversation_and_process()

    resp = client.put(f"/conversations/{conversation_id}/process", json={"process_id": process_id})
    assert resp.status_code == 200
    assert resp.json() == {"conversation_id": conversation_id, "process_id": process_id}


def test_set_conversation_process_missing_body_field_is_422(client: TestClient):
    _, conversation_id, _ = _seed_team_conversation_and_process()

    resp = client.put(f"/conversations/{conversation_id}/process", json={})
    assert resp.status_code == 422


def test_set_conversation_process_blank_process_id_is_422(client: TestClient):
    _, conversation_id, _ = _seed_team_conversation_and_process()

    resp = client.put(f"/conversations/{conversation_id}/process", json={"process_id": ""})
    assert resp.status_code == 422


def test_set_conversation_process_unknown_conversation_404(client: TestClient):
    resp = client.put("/conversations/does-not-exist/process", json={"process_id": "proc-1"})
    assert resp.status_code == 404


def test_set_conversation_process_unknown_process_404(client: TestClient):
    _, conversation_id, _ = _seed_team_conversation_and_process()

    resp = client.put(f"/conversations/{conversation_id}/process", json={"process_id": "nope"})
    assert resp.status_code == 404


def test_set_conversation_process_rejects_another_teams_process(client: TestClient):
    """A conversation may only be linked to a process owned by its own team,
    even if the process_id is otherwise valid (belongs to a different team)."""
    store = AgenticTeamStore()
    _, conversation_id, _ = _seed_team_conversation_and_process()

    other_team = store.create_team(name="Other Team", description="")
    other_process = ProcessDefinition(
        process_id="proc-other-team",
        name="Other",
        steps=[
            ProcessStep(
                step_id="s1",
                name="Do work",
                step_type=StepType.ACTION,
                agents=[ProcessStepAgent(agent_name="worker", role="doer")],
            )
        ],
    )
    store.save_process(other_team.team_id, other_process)

    resp = client.put(
        f"/conversations/{conversation_id}/process",
        json={"process_id": other_process.process_id},
    )
    assert resp.status_code == 403
    # Link left unchanged (still unset).
    assert store.get_conversation_process_id(conversation_id) is None
