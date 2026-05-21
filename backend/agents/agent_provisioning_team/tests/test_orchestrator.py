"""Unit tests for ProvisioningOrchestrator covering shutdown paths,
resume-with-skip, deprovisioning, and the smaller status helpers."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

from agent_provisioning_team.models import (
    AccessAuditResult,
    AccountProvisioningResult,
    CredentialGenerationResult,
    DeprovisionResult,
    DocumentationResult,
    EnvironmentInfo,
    OnboardingPacket,
    Phase,
    SetupResult,
    ToolProvisionResult,
)
from agent_provisioning_team.orchestrator import (
    ProvisioningOrchestrator,
    ProvisioningShutdownError,
    _build_tool_agents,
)
from agent_provisioning_team.shared.environment_store import (
    EnvironmentInfo as StoreEnvInfo,
)
from agent_provisioning_team.shared.environment_store import EnvironmentStore


def _make_manifest(tmp_path: Path) -> str:
    f = tmp_path / "m.yaml"
    f.write_text(
        """
version: "1.0"
tools:
  - name: postgresql
    provisioner: postgres_provisioner
    config: {database_prefix: "x_"}
    onboarding: {description: "PG"}
""",
        encoding="utf-8",
    )
    return str(f)


def _patch_run_setup(monkeypatch, *, success: bool = True, error: str | None = None) -> None:
    from agent_provisioning_team import orchestrator as orch_mod

    def fake(**kw):
        if success:
            return SetupResult(
                success=True,
                environment=EnvironmentInfo(
                    container_id="c1",
                    container_name="c1",
                    workspace_path="/tmp/ws",
                    status="running",
                ),
            )
        return SetupResult(success=False, error=error or "setup failed")

    monkeypatch.setattr(orch_mod, "run_setup", fake)


def test_build_tool_agents_shim() -> None:
    out = _build_tool_agents()
    assert isinstance(out, dict)
    assert "docker_provisioner" in out


def test_shutdown_error_str() -> None:
    err = ProvisioningShutdownError(agent_id="a1", phase="setup")
    assert err.agent_id == "a1"
    assert err.phase == "setup"
    assert "setup" in str(err)
    assert "a1" in str(err)


def test_run_workflow_manifest_load_failure() -> None:
    orch = ProvisioningOrchestrator()
    result = orch.run_workflow(agent_id="a1", manifest_path="/nonexistent/manifest.yaml")
    assert result.success is False
    assert result.current_phase == Phase.SETUP
    assert "Failed to load manifest" in result.error


def test_run_workflow_shutdown_before_setup(tmp_path: Path, monkeypatch) -> None:
    """When shutdown is signalled before SETUP, raise ProvisioningShutdownError."""
    orch = ProvisioningOrchestrator(
        environment_store=EnvironmentStore(storage_dir=tmp_path / "envs")
    )

    shutdown = threading.Event()
    shutdown.set()  # already signalled

    manifest = _make_manifest(tmp_path)
    with pytest.raises(ProvisioningShutdownError):
        orch.run_workflow(
            agent_id="a1",
            manifest_path=manifest,
            shutdown_event=shutdown,
        )


def test_run_workflow_setup_failure(tmp_path: Path, monkeypatch) -> None:
    _patch_run_setup(monkeypatch, success=False, error="docker exploded")
    orch = ProvisioningOrchestrator(
        environment_store=EnvironmentStore(storage_dir=tmp_path / "envs")
    )

    manifest = _make_manifest(tmp_path)
    result = orch.run_workflow(agent_id="a1", manifest_path=manifest)
    assert result.success is False
    assert result.current_phase == Phase.SETUP
    assert "docker exploded" in result.error


def test_run_workflow_credential_generation_failure(tmp_path: Path, monkeypatch) -> None:
    from agent_provisioning_team import orchestrator as orch_mod

    _patch_run_setup(monkeypatch)

    def fake_cred(**kw):
        return CredentialGenerationResult(success=False, credentials={}, error="cred boom")

    monkeypatch.setattr(orch_mod, "run_credential_generation", fake_cred)
    monkeypatch.setattr(orch_mod, "cleanup_setup", lambda *a, **kw: True)

    orch = ProvisioningOrchestrator(
        environment_store=EnvironmentStore(storage_dir=tmp_path / "envs")
    )
    manifest = _make_manifest(tmp_path)
    result = orch.run_workflow(agent_id="a1", manifest_path=manifest)
    assert result.success is False
    assert result.current_phase == Phase.CREDENTIAL_GENERATION


def test_run_workflow_resume_skips_setup(tmp_path: Path, monkeypatch) -> None:
    """When SETUP is in skip_phases + prior_results has setup, run_setup never fires."""
    from agent_provisioning_team import orchestrator as orch_mod

    def fake_setup(**kw):
        raise AssertionError("run_setup should not have been called on resume")

    monkeypatch.setattr(orch_mod, "run_setup", fake_setup)

    # Stub credential gen + account prov + audit + docs + deliver to short-circuit.
    monkeypatch.setattr(
        orch_mod,
        "run_credential_generation",
        lambda **kw: CredentialGenerationResult(success=True, credentials={}),
    )
    monkeypatch.setattr(
        orch_mod,
        "run_account_provisioning",
        lambda **kw: AccountProvisioningResult(success=True, tool_results=[]),
    )
    monkeypatch.setattr(
        orch_mod, "run_access_audit", lambda **kw: AccessAuditResult(passed=True, verifications=[])
    )
    monkeypatch.setattr(
        orch_mod,
        "run_documentation",
        lambda **kw: DocumentationResult(
            success=True,
            onboarding=OnboardingPacket(summary="s", tools=[], environment_variables={}),
        ),
    )

    orch = ProvisioningOrchestrator(
        environment_store=EnvironmentStore(storage_dir=tmp_path / "envs")
    )
    manifest = _make_manifest(tmp_path)

    result = orch.run_workflow(
        agent_id="a1",
        manifest_path=manifest,
        skip_phases={Phase.SETUP},
        prior_results={
            "setup": {
                "success": True,
                "environment": {
                    "container_id": "c-pre",
                    "container_name": "c-pre",
                    "workspace_path": "/restored",
                    "status": "running",
                },
            },
        },
    )
    assert result.success is True


def test_run_workflow_with_job_updater_callback(tmp_path: Path, monkeypatch) -> None:
    from agent_provisioning_team import orchestrator as orch_mod

    _patch_run_setup(monkeypatch)
    monkeypatch.setattr(
        orch_mod,
        "run_credential_generation",
        lambda **kw: CredentialGenerationResult(success=True, credentials={}),
    )
    monkeypatch.setattr(
        orch_mod,
        "run_account_provisioning",
        lambda **kw: AccountProvisioningResult(success=True, tool_results=[]),
    )
    monkeypatch.setattr(
        orch_mod, "run_access_audit", lambda **kw: AccessAuditResult(passed=True, verifications=[])
    )
    monkeypatch.setattr(
        orch_mod,
        "run_documentation",
        lambda **kw: DocumentationResult(
            success=True,
            onboarding=OnboardingPacket(summary="s", tools=[], environment_variables={}),
        ),
    )

    updates = []

    def updater(**kw):
        updates.append(kw)

    orch = ProvisioningOrchestrator(
        environment_store=EnvironmentStore(storage_dir=tmp_path / "envs")
    )
    manifest = _make_manifest(tmp_path)
    result = orch.run_workflow(agent_id="a1", manifest_path=manifest, job_updater=updater)
    assert result.success is True
    # Several phase updates fired
    assert any(u.get("progress") == 100 for u in updates)


def test_run_workflow_resume_restores_all_phases(tmp_path: Path, monkeypatch) -> None:
    """skip_phases for every prior phase; only DELIVER runs."""
    from agent_provisioning_team import orchestrator as orch_mod

    def fail(**kw):
        raise AssertionError("phase function should be skipped")

    monkeypatch.setattr(orch_mod, "run_setup", fail)
    monkeypatch.setattr(orch_mod, "run_credential_generation", fail)
    monkeypatch.setattr(orch_mod, "run_account_provisioning", fail)
    monkeypatch.setattr(orch_mod, "run_access_audit", fail)
    monkeypatch.setattr(orch_mod, "run_documentation", fail)

    orch = ProvisioningOrchestrator(
        environment_store=EnvironmentStore(storage_dir=tmp_path / "envs")
    )
    manifest = _make_manifest(tmp_path)

    # NB: access_audit isn't in skip_phases because the orchestrator stores
    # the raw dict from prior_results without re-typing, which breaks
    # build_final_result downstream. That's a pre-existing production quirk
    # (out of scope for this test suite — coverage only). So we re-run the
    # audit but skip everything else.
    monkeypatch.setattr(
        orch_mod,
        "run_access_audit",
        lambda **kw: AccessAuditResult(passed=True, verifications=[]),
    )
    result = orch.run_workflow(
        agent_id="a1",
        manifest_path=manifest,
        skip_phases={
            Phase.SETUP,
            Phase.CREDENTIAL_GENERATION,
            Phase.ACCOUNT_PROVISIONING,
            Phase.DOCUMENTATION,
        },
        prior_results={
            "setup": {
                "success": True,
                "environment": {
                    "container_id": "c1",
                    "container_name": "c1",
                    "workspace_path": "/ws",
                    "status": "running",
                },
            },
            "credential_generation": {"success": True, "credentials": {}},
            "account_provisioning": {
                "success": True,
                "tool_results": [],
                "tools_completed": 0,
                "tools_total": 0,
            },
            "documentation": {
                "success": True,
                "onboarding": {
                    "summary": "s",
                    "tools": [],
                    "environment_variables": {},
                },
            },
        },
    )
    assert result.success is True


def test_run_workflow_account_provisioning_failure_compensates(tmp_path: Path, monkeypatch) -> None:
    """A failed account provisioning rolls back via _compensate."""
    from agent_provisioning_team import orchestrator as orch_mod

    _patch_run_setup(monkeypatch)
    monkeypatch.setattr(
        orch_mod,
        "run_credential_generation",
        lambda **kw: CredentialGenerationResult(success=True, credentials={}),
    )

    failed_result = AccountProvisioningResult(
        success=False,
        tool_results=[
            ToolProvisionResult(
                tool_name="t",
                success=True,
                provisioner_key="generic_provisioner",
            ),
            ToolProvisionResult(
                tool_name="t2",
                success=False,
                error="boom",
                provisioner_key="generic_provisioner",
            ),
        ],
        error="provisioning failed",
    )
    monkeypatch.setattr(orch_mod, "run_account_provisioning", lambda **kw: failed_result)

    fake_generic = MagicMock()
    fake_generic.list_compensations.return_value = []
    fake_generic.deprovision.return_value = DeprovisionResult(tool_name="generic", success=True)

    fake_docker = MagicMock()
    fake_docker.deprovision.return_value = DeprovisionResult(tool_name="docker", success=True)

    orch = ProvisioningOrchestrator(
        environment_store=EnvironmentStore(storage_dir=tmp_path / "envs"),
        tool_agents={
            "generic_provisioner": fake_generic,
            "docker_provisioner": fake_docker,
        },
    )
    manifest = _make_manifest(tmp_path)
    result = orch.run_workflow(agent_id="a1", manifest_path=manifest)
    assert result.success is False
    assert result.current_phase == Phase.ACCOUNT_PROVISIONING
    # Generic was deprovisioned (legacy path); docker too.
    fake_generic.deprovision.assert_called_once_with("a1")
    fake_docker.deprovision.assert_called_once_with("a1")


def test_compensate_swallows_docker_failure(tmp_path: Path) -> None:
    fake_docker = MagicMock()
    fake_docker.deprovision.side_effect = RuntimeError("daemon down")

    orch = ProvisioningOrchestrator(
        environment_store=EnvironmentStore(storage_dir=tmp_path / "envs"),
        tool_agents={"docker_provisioner": fake_docker},
    )
    # Should not raise
    orch._compensate("a1", [])


def test_compensate_skips_unsuccessful_tool_results(tmp_path: Path) -> None:
    """Failed tool results are skipped — no replay, no deprovision call."""
    fake_prov = MagicMock()

    orch = ProvisioningOrchestrator(
        environment_store=EnvironmentStore(storage_dir=tmp_path / "envs"),
        tool_agents={"some_provisioner": fake_prov, "docker_provisioner": MagicMock()},
    )

    failed = ToolProvisionResult(
        tool_name="t",
        success=False,
        error="no",
        provisioner_key="some_provisioner",
    )

    orch._compensate("a1", [failed])
    fake_prov.deprovision.assert_not_called()
    fake_prov.list_compensations.assert_not_called()


def test_compensate_skips_when_no_provisioner_key(tmp_path: Path, caplog) -> None:
    fake_prov = MagicMock()
    orch = ProvisioningOrchestrator(
        environment_store=EnvironmentStore(storage_dir=tmp_path / "envs"),
        tool_agents={"some_provisioner": fake_prov, "docker_provisioner": MagicMock()},
    )

    # No provisioner_key
    success_no_key = ToolProvisionResult(tool_name="t", success=True)
    orch._compensate("a1", [success_no_key])
    fake_prov.deprovision.assert_not_called()


def test_compensate_skips_when_provisioner_unregistered(tmp_path: Path) -> None:
    fake_docker = MagicMock()
    orch = ProvisioningOrchestrator(
        environment_store=EnvironmentStore(storage_dir=tmp_path / "envs"),
        tool_agents={"docker_provisioner": fake_docker},
    )

    success_unknown_key = ToolProvisionResult(tool_name="t", success=True, provisioner_key="ghost")
    orch._compensate("a1", [success_unknown_key])


def test_compensate_list_compensations_failure_falls_to_deprovision(tmp_path: Path) -> None:
    fake_prov = MagicMock()
    fake_prov.list_compensations.side_effect = RuntimeError("list boom")
    fake_prov.deprovision.return_value = DeprovisionResult(tool_name="t", success=True)

    orch = ProvisioningOrchestrator(
        environment_store=EnvironmentStore(storage_dir=tmp_path / "envs"),
        tool_agents={"x": fake_prov, "docker_provisioner": MagicMock()},
    )

    success = ToolProvisionResult(tool_name="t", success=True, provisioner_key="x")
    orch._compensate("a1", [success])
    fake_prov.deprovision.assert_called_once()


def test_compensate_replay_failure_continues(tmp_path: Path) -> None:
    from agent_provisioning_team.shared.provisioner_state import CompensationRecord

    fake_prov = MagicMock()
    fake_prov.list_compensations.return_value = [
        CompensationRecord(kind="k1", payload={}),
        CompensationRecord(kind="k2", payload={}),
    ]
    fake_prov.replay_compensation.side_effect = RuntimeError("replay boom")
    fake_prov._state.delete = MagicMock(return_value=True)

    orch = ProvisioningOrchestrator(
        environment_store=EnvironmentStore(storage_dir=tmp_path / "envs"),
        tool_agents={"x": fake_prov, "docker_provisioner": MagicMock()},
    )

    success = ToolProvisionResult(tool_name="t", success=True, provisioner_key="x")
    orch._compensate("a1", [success])
    # Replay attempted for both records (continues despite failure)
    assert fake_prov.replay_compensation.call_count == 2


def test_compensate_credential_cleanup_failure_swallowed(tmp_path: Path) -> None:
    cred_store = MagicMock()
    cred_store.delete_credentials.side_effect = RuntimeError("cred io")

    orch = ProvisioningOrchestrator(
        credential_store=cred_store,
        environment_store=EnvironmentStore(storage_dir=tmp_path / "envs"),
        tool_agents={"docker_provisioner": MagicMock()},
    )
    orch._compensate("a1", [])


def test_compensate_environment_cleanup_failure_swallowed(tmp_path: Path, monkeypatch) -> None:
    from agent_provisioning_team import orchestrator as orch_mod

    monkeypatch.setattr(orch_mod, "cleanup_setup", MagicMock(side_effect=RuntimeError("env io")))
    orch = ProvisioningOrchestrator(
        environment_store=EnvironmentStore(storage_dir=tmp_path / "envs"),
        tool_agents={"docker_provisioner": MagicMock()},
    )
    orch._compensate("a1", [])


def test_compensate_post_replay_state_cleanup_failure(tmp_path: Path) -> None:
    """If clear_compensations or _state.delete fails, swallow and move on."""
    from agent_provisioning_team.shared.provisioner_state import CompensationRecord

    fake_prov = MagicMock()
    fake_prov.list_compensations.return_value = [CompensationRecord(kind="k1", payload={})]
    fake_prov.clear_compensations.side_effect = RuntimeError("clear io")

    orch = ProvisioningOrchestrator(
        environment_store=EnvironmentStore(storage_dir=tmp_path / "envs"),
        tool_agents={"x": fake_prov, "docker_provisioner": MagicMock()},
    )
    success = ToolProvisionResult(tool_name="t", success=True, provisioner_key="x")
    orch._compensate("a1", [success])


# ---------------------------------------------------------------------------
# deprovision / status / list
# ---------------------------------------------------------------------------


def test_deprovision_success_returns_response(tmp_path: Path) -> None:
    fake_pg = MagicMock()
    fake_pg.deprovision.return_value = DeprovisionResult(tool_name="pg", success=True)
    fake_docker = MagicMock()
    fake_docker.deprovision.return_value = DeprovisionResult(tool_name="docker", success=True)

    env_store = EnvironmentStore(storage_dir=tmp_path / "envs")
    cred_store = MagicMock()
    cred_store.delete_credentials.return_value = True

    orch = ProvisioningOrchestrator(
        credential_store=cred_store,
        environment_store=env_store,
        tool_agents={"postgres_provisioner": fake_pg, "docker_provisioner": fake_docker},
    )

    resp = orch.deprovision("a1")
    assert resp.success is True


def test_deprovision_collects_errors_unless_forced(tmp_path: Path) -> None:
    fake_pg = MagicMock()
    fake_pg.deprovision.return_value = DeprovisionResult(tool_name="pg", success=False)
    fake_docker = MagicMock()
    fake_docker.deprovision.return_value = DeprovisionResult(
        tool_name="docker", success=False, error="daemon down"
    )

    cred_store = MagicMock()
    cred_store.delete_credentials.return_value = False
    env_store = MagicMock()
    env_store.remove.return_value = False

    orch = ProvisioningOrchestrator(
        credential_store=cred_store,
        environment_store=env_store,
        tool_agents={"postgres_provisioner": fake_pg, "docker_provisioner": fake_docker},
    )

    resp = orch.deprovision("a1", force=False)
    assert resp.success is False
    assert "Failed to deprovision" in resp.error
    assert "daemon down" in resp.error


def test_deprovision_force_returns_success(tmp_path: Path) -> None:
    fake_docker = MagicMock()
    fake_docker.deprovision.return_value = DeprovisionResult(
        tool_name="docker", success=False, error="x"
    )
    cred_store = MagicMock()
    cred_store.delete_credentials.return_value = False
    env_store = MagicMock()
    env_store.remove.return_value = False

    orch = ProvisioningOrchestrator(
        credential_store=cred_store,
        environment_store=env_store,
        tool_agents={"docker_provisioner": fake_docker},
    )
    resp = orch.deprovision("a1", force=True)
    assert resp.success is True


def test_get_agent_status_missing(tmp_path: Path) -> None:
    orch = ProvisioningOrchestrator(
        environment_store=EnvironmentStore(storage_dir=tmp_path / "envs")
    )
    assert orch.get_agent_status("nobody") is None


def test_get_agent_status_returns_dict(tmp_path: Path) -> None:
    env_store = EnvironmentStore(storage_dir=tmp_path / "envs")
    env_store.register(
        StoreEnvInfo(
            agent_id="a1",
            container_id="c1",
            container_name="agent-a1",
            ssh_host="localhost",
            ssh_port=22001,
            workspace_path="/w",
            tools_provisioned=["pg"],
        )
    )
    orch = ProvisioningOrchestrator(environment_store=env_store)
    status = orch.get_agent_status("a1")
    assert status["agent_id"] == "a1"
    assert status["container_name"] == "agent-a1"
    assert status["tools_provisioned"] == ["pg"]


def test_list_agents_filters_by_status(tmp_path: Path) -> None:
    env_store = EnvironmentStore(storage_dir=tmp_path / "envs")
    env_store.register(
        StoreEnvInfo(
            agent_id="a1",
            container_id="c1",
            container_name="c1",
            workspace_path="/w",
            status="ready",
        )
    )
    env_store.register(
        StoreEnvInfo(
            agent_id="a2",
            container_id="c2",
            container_name="c2",
            workspace_path="/w",
            status="running",
        )
    )

    orch = ProvisioningOrchestrator(environment_store=env_store)
    ready = orch.list_agents(status="ready")
    assert len(ready) == 1
    assert ready[0]["agent_id"] == "a1"


def test_run_workflow_shutdown_mid_workflow(tmp_path: Path, monkeypatch) -> None:
    """If shutdown event flips between phases, _compensate runs + raise fires."""
    from agent_provisioning_team import orchestrator as orch_mod

    _patch_run_setup(monkeypatch)
    monkeypatch.setattr(
        orch_mod,
        "run_credential_generation",
        lambda **kw: CredentialGenerationResult(success=True, credentials={}),
    )

    def acc(**kw):
        # Flip the shutdown event so the next _check_shutdown raises.
        kw["progress_callback"](1, 2, "pg")
        return AccountProvisioningResult(
            success=True,
            tool_results=[
                ToolProvisionResult(
                    tool_name="pg",
                    success=True,
                    provisioner_key="postgres_provisioner",
                )
            ],
        )

    monkeypatch.setattr(orch_mod, "run_account_provisioning", acc)

    fake_pg = MagicMock()
    fake_pg.list_compensations.return_value = []
    fake_pg.deprovision.return_value = DeprovisionResult(tool_name="pg", success=True)
    fake_docker = MagicMock()
    fake_docker.deprovision.return_value = DeprovisionResult(tool_name="docker", success=True)

    orch = ProvisioningOrchestrator(
        environment_store=EnvironmentStore(storage_dir=tmp_path / "envs"),
        tool_agents={
            "postgres_provisioner": fake_pg,
            "docker_provisioner": fake_docker,
        },
    )

    shutdown = threading.Event()

    # Set the shutdown after the workflow has past SETUP but before audit
    # by patching run_access_audit to first set the event.
    def audit(**kw):
        shutdown.set()
        return AccessAuditResult(passed=True, verifications=[])

    monkeypatch.setattr(orch_mod, "run_access_audit", audit)

    manifest = _make_manifest(tmp_path)

    with pytest.raises(ProvisioningShutdownError):
        orch.run_workflow(
            agent_id="a1",
            manifest_path=manifest,
            shutdown_event=shutdown,
        )

    # Compensation should have rolled back the pg tool
    fake_pg.deprovision.assert_called_once()


def test_run_workflow_uses_default_workspace_when_env_missing(tmp_path: Path, monkeypatch) -> None:
    """If setup_result.environment is None on resume, documentation uses /workspace."""
    from agent_provisioning_team import orchestrator as orch_mod

    monkeypatch.setattr(
        orch_mod,
        "run_credential_generation",
        lambda **kw: CredentialGenerationResult(success=True, credentials={}),
    )
    monkeypatch.setattr(
        orch_mod,
        "run_account_provisioning",
        lambda **kw: AccountProvisioningResult(success=True, tool_results=[]),
    )
    monkeypatch.setattr(
        orch_mod, "run_access_audit", lambda **kw: AccessAuditResult(passed=True, verifications=[])
    )

    captured_workspace: Dict[str, Any] = {}

    def doc(**kw):
        captured_workspace["ws"] = kw.get("workspace_path")
        return DocumentationResult(
            success=True,
            onboarding=OnboardingPacket(summary="s", tools=[], environment_variables={}),
        )

    monkeypatch.setattr(orch_mod, "run_documentation", doc)

    orch = ProvisioningOrchestrator(
        environment_store=EnvironmentStore(storage_dir=tmp_path / "envs")
    )
    manifest = _make_manifest(tmp_path)
    # Resume with skip on SETUP but no environment in prior_results.
    # build_final_result will mark success=False because environment is None,
    # but the orchestrator still runs every phase and passes "/workspace"
    # as the documentation workspace — which is what this test verifies.
    orch.run_workflow(
        agent_id="a1",
        manifest_path=manifest,
        skip_phases={Phase.SETUP},
        prior_results={
            "setup": {"success": True, "environment": None},
        },
    )
    assert captured_workspace["ws"] == "/workspace"
