"""Tests for bridge to agent_provisioning_team."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agent_team_studio.agentic_team_provisioning.agent_env_provisioning import (
    _acquire_lock_blocking,
    _provision_one,
    make_provisioning_agent_id,
    schedule_provision_step_agents,
)
from agent_team_studio.agentic_team_provisioning.assistant.store import AgenticTeamStore
from agent_team_studio.agentic_team_provisioning.models import (
    ProcessDefinition,
    ProcessOutput,
    ProcessStatus,
    ProcessStep,
    ProcessStepAgent,
    ProcessTrigger,
    TriggerType,
)
from agent_team_studio.agentic_team_provisioning.tests._fake_postgres import install_fake_postgres


@pytest.fixture
def fake_pg(monkeypatch: pytest.MonkeyPatch) -> dict:
    return install_fake_postgres(monkeypatch)


def test_make_provisioning_agent_id_stable():
    a = make_provisioning_agent_id("team-uuid-1", "proc-2", "step_1", "Triage Agent")
    b = make_provisioning_agent_id("team-uuid-1", "proc-2", "step_1", "Triage Agent")
    assert a == b
    assert a.startswith("at-")
    assert len(a) <= 120


def test_make_provisioning_agent_id_slugs_step_and_agent_segments():
    # step_id/agent_name segments are lowercased and hyphenated (shared `slug()`
    # semantics); team_id/process_id segments are alphanumeric-stripped only. This
    # locks the exact format so a future refactor can't silently change it.
    agent_id = make_provisioning_agent_id("Team-1", "Proc.2", "Step One!", "Triage Agent")
    assert agent_id == "at-Team1-Proc2-step-one-triage-agent"


def test_schedule_provision_skips_when_disabled(monkeypatch, fake_pg: dict):
    monkeypatch.setenv("AGENTIC_TEAM_AGENT_PROVISIONING_ENABLED", "false")
    # Reload module flag
    import agent_team_studio.agentic_team_provisioning.agent_env_provisioning as mod

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
    import agent_team_studio.agentic_team_provisioning.agent_env_provisioning as mod

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


class _FakeResult:
    def __init__(self, success: bool, error: str | None = None):
        self.success = success
        self.error = error


def test_provision_one_success_acquires_and_releases_lock(fake_pg: dict):
    store = MagicMock()
    fake_orch = MagicMock()
    fake_orch.run_workflow.return_value = _FakeResult(success=True)
    fake_lock_store = MagicMock()

    with (
        patch(
            "agent_team_studio.agent_provisioning_team.orchestrator.ProvisioningOrchestrator",
            return_value=fake_orch,
        ),
        patch(
            "agent_team_studio.agent_provisioning_team.shared.agent_lock.AgentLockStore",
            return_value=fake_lock_store,
        ),
    ):
        _provision_one(
            team_id="t1",
            stable_key="p1:s1:A1",
            provisioning_agent_id="at-test-id",
            store=store,
        )

    fake_lock_store.acquire.assert_called_once()
    assert fake_lock_store.acquire.call_args.args[0] == "at-test-id"
    fake_lock_store.release.assert_called_once()
    assert fake_lock_store.release.call_args.args[0] == "at-test-id"
    # The release owner must match the owner acquire claimed with.
    assert fake_lock_store.release.call_args.args[1] == fake_lock_store.acquire.call_args.args[1]
    store.mark_agent_env_provision_finished.assert_called_once_with(
        "t1", "p1:s1:A1", success=True, error_message=None
    )


def test_provision_one_threads_fencing_token(fake_pg: dict):
    """The token minted by acquire() must reach both run_workflow() and the
    final release() -- this path has no renewal loop (single synchronous
    run_workflow call under one lease), so the one captured value is used
    throughout."""
    store = MagicMock()
    fake_orch = MagicMock()
    fake_orch.run_workflow.return_value = _FakeResult(success=True)
    fake_lock_store = MagicMock()
    fake_lock_store.acquire.return_value = 7

    with (
        patch(
            "agent_team_studio.agent_provisioning_team.orchestrator.ProvisioningOrchestrator",
            return_value=fake_orch,
        ),
        patch(
            "agent_team_studio.agent_provisioning_team.shared.agent_lock.AgentLockStore",
            return_value=fake_lock_store,
        ),
    ):
        _provision_one(
            team_id="t1",
            stable_key="p1:s1:A1",
            provisioning_agent_id="at-test-id",
            store=store,
        )

    assert fake_orch.run_workflow.call_args.kwargs["fencing_token"] == 7
    assert fake_lock_store.release.call_args.kwargs["fencing_token"] == 7


def test_provision_one_releases_lock_when_orchestrator_raises(fake_pg: dict):
    store = MagicMock()
    fake_orch = MagicMock()
    fake_orch.run_workflow.side_effect = RuntimeError("boom")
    fake_lock_store = MagicMock()

    with (
        patch(
            "agent_team_studio.agent_provisioning_team.orchestrator.ProvisioningOrchestrator",
            return_value=fake_orch,
        ),
        patch(
            "agent_team_studio.agent_provisioning_team.shared.agent_lock.AgentLockStore",
            return_value=fake_lock_store,
        ),
    ):
        _provision_one(
            team_id="t1",
            stable_key="p1:s1:A1",
            provisioning_agent_id="at-test-id",
            store=store,
        )

    fake_lock_store.release.assert_called_once()
    store.mark_agent_env_provision_finished.assert_called_once_with(
        "t1", "p1:s1:A1", success=False, error_message="boom"
    )


def test_provision_one_skips_orchestrator_when_lock_busy(fake_pg: dict):
    from agent_team_studio.agent_provisioning_team.shared.agent_lock import AgentLockBusyError

    store = MagicMock()
    fake_orch = MagicMock()
    fake_lock_store = MagicMock()
    fake_lock_store.acquire.side_effect = AgentLockBusyError("at-test-id", "other-owner")

    with (
        patch(
            "agent_team_studio.agent_provisioning_team.orchestrator.ProvisioningOrchestrator",
            return_value=fake_orch,
        ),
        patch(
            "agent_team_studio.agent_provisioning_team.shared.agent_lock.AgentLockStore",
            return_value=fake_lock_store,
        ),
        patch("agent_team_studio.agentic_team_provisioning.agent_env_provisioning.time.sleep"),
        patch(
            "agent_team_studio.agent_provisioning_team.temporal.constants.LOCK_ACQUIRE_TIMEOUT_S",
            0.01,
        ),
    ):
        _provision_one(
            team_id="t1",
            stable_key="p1:s1:A1",
            provisioning_agent_id="at-test-id",
            store=store,
        )

    fake_orch.run_workflow.assert_not_called()
    fake_lock_store.release.assert_not_called()
    args, kwargs = store.mark_agent_env_provision_finished.call_args
    assert args[:2] == ("t1", "p1:s1:A1")
    assert kwargs["success"] is False
    assert "at-test-id" in kwargs["error_message"]


def test_acquire_lock_blocking_returns_once_acquired():
    lock_store = MagicMock()
    lock_store.acquire.return_value = None

    _acquire_lock_blocking(lock_store, "agent-1", "owner-1", timeout_s=5)

    lock_store.acquire.assert_called_once_with("agent-1", "owner-1")


def test_acquire_lock_blocking_returns_fencing_token():
    lock_store = MagicMock()
    lock_store.acquire.return_value = 3

    token = _acquire_lock_blocking(lock_store, "agent-1", "owner-1", timeout_s=5)

    assert token == 3


def test_acquire_lock_blocking_raises_after_timeout():
    from agent_team_studio.agent_provisioning_team.shared.agent_lock import AgentLockBusyError

    lock_store = MagicMock()
    lock_store.acquire.side_effect = AgentLockBusyError("agent-1", "other-owner")

    with patch("agent_team_studio.agentic_team_provisioning.agent_env_provisioning.time.sleep"):
        with pytest.raises(AgentLockBusyError):
            _acquire_lock_blocking(lock_store, "agent-1", "owner-1", timeout_s=0.01)

    assert lock_store.acquire.call_count >= 1


def test_acquire_lock_blocking_rejects_non_positive_timeout():
    lock_store = MagicMock()
    with pytest.raises(AssertionError):
        _acquire_lock_blocking(lock_store, "agent-1", "owner-1", timeout_s=0)


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
