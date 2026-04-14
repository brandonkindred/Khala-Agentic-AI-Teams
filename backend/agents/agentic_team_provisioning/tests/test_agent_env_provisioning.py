"""Tests for bridge to agent_provisioning_team."""

from __future__ import annotations

import pytest

from agentic_team_provisioning.agent_env_provisioning import (
    make_provisioning_agent_id,
    schedule_provision_step_agents,
)
from agentic_team_provisioning.assistant.store import AgenticTeamStore
from agentic_team_provisioning.models import (
    ProcessDefinition,
    ProcessOutput,
    ProcessStatus,
    ProcessStep,
    ProcessStepAgent,
    ProcessTrigger,
    TriggerType,
)
from agentic_team_provisioning.tests._fake_postgres import install_fake_postgres


@pytest.fixture
def fake_pg(monkeypatch: pytest.MonkeyPatch) -> dict:
    return install_fake_postgres(monkeypatch)


def test_make_provisioning_agent_id_stable():
    a = make_provisioning_agent_id("team-uuid-1", "proc-2", "step_1", "Triage Agent")
    b = make_provisioning_agent_id("team-uuid-1", "proc-2", "step_1", "Triage Agent")
    assert a == b
    assert a.startswith("at-")
    assert len(a) <= 120


def test_schedule_provision_skips_when_disabled(monkeypatch, fake_pg: dict):
    monkeypatch.setenv("AGENTIC_TEAM_AGENT_PROVISIONING_ENABLED", "false")
    # Reload module flag
    import agentic_team_provisioning.agent_env_provisioning as mod

    monkeypatch.setattr(mod, "_ENABLED", False)

    store = AgenticTeamStore()
    team = store.create_team(name="T", description="")
    proc = ProcessDefinition(
        process_id="p1",
        name="P",
        description="",
        trigger=ProcessTrigger(trigger_type=TriggerType.MESSAGE, description=""),
        steps=[
            ProcessStep(
                step_id="s1",
                name="S",
                description="",
                agents=[ProcessStepAgent(agent_name="A1", role="r")],
            )
        ],
        output=ProcessOutput(description="", destination=""),
        status=ProcessStatus.DRAFT,
    )
    schedule_provision_step_agents(team.team_id, proc, store)
    assert store.list_agent_env_provisions(team.team_id) == []


def test_try_begin_and_list(monkeypatch, fake_pg: dict):
    monkeypatch.setenv("AGENTIC_TEAM_AGENT_PROVISIONING_ENABLED", "false")
    import agentic_team_provisioning.agent_env_provisioning as mod

    monkeypatch.setattr(mod, "_ENABLED", False)

    store = AgenticTeamStore()
    team = store.create_team(name="T", description="")

    ok = store.try_begin_agent_env_provision(
        team_id=team.team_id,
        stable_key="p1:s1:A1",
        process_id="p1",
        step_id="s1",
        agent_name="A1",
        provisioning_agent_id="at-test-id",
    )
    assert ok is True

    ok2 = store.try_begin_agent_env_provision(
        team_id=team.team_id,
        stable_key="p1:s1:A1",
        process_id="p1",
        step_id="s1",
        agent_name="A1",
        provisioning_agent_id="at-test-id-2",
    )
    assert ok2 is False

    rows = store.list_agent_env_provisions(team.team_id)
    assert len(rows) == 1
    assert rows[0]["status"] == "running"

    store.mark_agent_env_provision_finished(
        team.team_id, "p1:s1:A1", success=True, error_message=None
    )
    rows2 = store.list_agent_env_provisions(team.team_id)
    assert rows2[0]["status"] == "completed"


def test_try_begin_retries_after_failure(fake_pg: dict):
    """After a provisioning attempt fails, a subsequent try_begin re-runs it."""
    store = AgenticTeamStore()
    team = store.create_team(name="T", description="")

    assert (
        store.try_begin_agent_env_provision(
            team_id=team.team_id,
            stable_key="p1:s1:A1",
            process_id="p1",
            step_id="s1",
            agent_name="A1",
            provisioning_agent_id="at-first",
        )
        is True
    )
    store.mark_agent_env_provision_finished(
        team.team_id, "p1:s1:A1", success=False, error_message="boom"
    )
    # Now that the row is 'failed', a new caller should be granted the right to retry.
    assert (
        store.try_begin_agent_env_provision(
            team_id=team.team_id,
            stable_key="p1:s1:A1",
            process_id="p1",
            step_id="s1",
            agent_name="A1",
            provisioning_agent_id="at-retry",
        )
        is True
    )
