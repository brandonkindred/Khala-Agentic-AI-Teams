"""Unit tests for ProvisioningOrchestrator covering shutdown paths,
resume-with-skip, deprovisioning, and the smaller status helpers."""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, call

import pytest

from agent_team_studio.agent_provisioning_team.models import (
    AccessAuditResult,
    AccountProvisioningResult,
    CredentialGenerationResult,
    DeliverResult,
    DeprovisionResult,
    DocumentationResult,
    EnvironmentInfo,
    OnboardingPacket,
    Phase,
    SetupResult,
    ToolProvisionResult,
)
from agent_team_studio.agent_provisioning_team.orchestrator import (
    ProvisioningOrchestrator,
    ProvisioningShutdownError,
)
from agent_team_studio.agent_provisioning_team.shared.environment_store import (
    EnvironmentInfo as StoreEnvInfo,
)
from agent_team_studio.agent_provisioning_team.shared.environment_store import EnvironmentStore
from agent_team_studio.agent_provisioning_team.shared.tool_agent_registry import (
    build_default_tool_agents,
)


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
    from agent_team_studio.agent_provisioning_team import orchestrator as orch_mod

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


def test_build_default_tool_agents_includes_docker() -> None:
    out = build_default_tool_agents()
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
    from agent_team_studio.agent_provisioning_team import orchestrator as orch_mod

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
    from agent_team_studio.agent_provisioning_team import orchestrator as orch_mod

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
    from agent_team_studio.agent_provisioning_team import orchestrator as orch_mod

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


def test_run_workflow_threads_fencing_token_to_every_phase(tmp_path: Path, monkeypatch) -> None:
    from agent_team_studio.agent_provisioning_team import orchestrator as orch_mod

    captured: Dict[str, Any] = {}

    def _capturing(name):
        def _fake(**kw):
            captured[name] = kw
            return {
                "run_setup": SetupResult(
                    success=True,
                    environment=EnvironmentInfo(
                        container_id="c1", container_name="c1", workspace_path="/tmp/ws"
                    ),
                ),
                "run_credential_generation": CredentialGenerationResult(
                    success=True, credentials={}
                ),
                "run_account_provisioning": AccountProvisioningResult(
                    success=True, tool_results=[]
                ),
                "run_access_audit": AccessAuditResult(passed=True, verifications=[]),
                "run_documentation": DocumentationResult(
                    success=True,
                    onboarding=OnboardingPacket(summary="s", tools=[], environment_variables={}),
                ),
                "run_deliver": DeliverResult(success=True, finalized_at=datetime.now(timezone.utc)),
            }[name]

        return _fake

    for name in (
        "run_setup",
        "run_credential_generation",
        "run_account_provisioning",
        "run_access_audit",
        "run_documentation",
        "run_deliver",
    ):
        monkeypatch.setattr(orch_mod, name, _capturing(name))

    orch = ProvisioningOrchestrator(
        environment_store=EnvironmentStore(storage_dir=tmp_path / "envs")
    )
    manifest = _make_manifest(tmp_path)
    result = orch.run_workflow(agent_id="a1", manifest_path=manifest, fencing_token=11)

    assert result.success is True
    for name in (
        "run_setup",
        "run_credential_generation",
        "run_account_provisioning",
        "run_deliver",
    ):
        assert captured[name]["fencing_token"] == 11, f"{name} did not receive fencing_token"


def test_run_workflow_resume_restores_all_phases(tmp_path: Path, monkeypatch) -> None:
    """skip_phases for every prior phase; only DELIVER runs."""
    from agent_team_studio.agent_provisioning_team import orchestrator as orch_mod

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

    result = orch.run_workflow(
        agent_id="a1",
        manifest_path=manifest,
        skip_phases={
            Phase.SETUP,
            Phase.CREDENTIAL_GENERATION,
            Phase.ACCOUNT_PROVISIONING,
            Phase.ACCESS_AUDIT,
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
            "access_audit": {"passed": True, "verifications": []},
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
    # Regression: the resumed access_audit must be re-typed into
    # AccessAuditResult, not left as the raw prior_results dict — otherwise
    # build_final_result's `access_audit.passed` attribute access would have
    # raised AttributeError before result.success was reached.
    assert isinstance(result.access_audit, AccessAuditResult)
    assert result.access_audit.passed is True


def test_run_workflow_account_provisioning_failure_compensates(tmp_path: Path, monkeypatch) -> None:
    """A failed account provisioning rolls back via compensate."""
    from agent_team_studio.agent_provisioning_team import orchestrator as orch_mod

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
    fake_generic.deprovision.assert_called_once_with("a1", fencing_token=None)
    fake_docker.deprovision.assert_called_once_with("a1", fencing_token=None)


def test_compensate_swallows_docker_failure(tmp_path: Path) -> None:
    fake_docker = MagicMock()
    fake_docker.deprovision.side_effect = RuntimeError("daemon down")

    orch = ProvisioningOrchestrator(
        environment_store=EnvironmentStore(storage_dir=tmp_path / "envs"),
        tool_agents={"docker_provisioner": fake_docker},
    )
    # Should not raise
    orch.compensate("a1", [])


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

    orch.compensate("a1", [failed])
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
    orch.compensate("a1", [success_no_key])
    fake_prov.deprovision.assert_not_called()


def test_compensate_skips_when_provisioner_unregistered(tmp_path: Path) -> None:
    fake_docker = MagicMock()
    orch = ProvisioningOrchestrator(
        environment_store=EnvironmentStore(storage_dir=tmp_path / "envs"),
        tool_agents={"docker_provisioner": fake_docker},
    )

    success_unknown_key = ToolProvisionResult(tool_name="t", success=True, provisioner_key="ghost")
    orch.compensate("a1", [success_unknown_key])
    # Unknown registry key skips per-tool rollback; docker/env teardown still runs.
    fake_docker.deprovision.assert_called_once_with("a1", fencing_token=None)


def test_compensate_list_compensations_failure_falls_to_deprovision(tmp_path: Path) -> None:
    fake_prov = MagicMock()
    fake_prov.list_compensations.side_effect = RuntimeError("list boom")
    fake_prov.deprovision.return_value = DeprovisionResult(tool_name="t", success=True)

    orch = ProvisioningOrchestrator(
        environment_store=EnvironmentStore(storage_dir=tmp_path / "envs"),
        tool_agents={"x": fake_prov, "docker_provisioner": MagicMock()},
    )

    success = ToolProvisionResult(tool_name="t", success=True, provisioner_key="x")
    orch.compensate("a1", [success])
    fake_prov.deprovision.assert_called_once()


def test_compensate_replay_failure_continues(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.shared.provisioner_state import (
        CompensationRecord,
    )

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
    orch.compensate("a1", [success])
    # Replay attempted for both records (continues despite failure)
    assert fake_prov.replay_compensation.call_count == 2
    # A failed replay step preserves the persisted records/state instead of
    # clearing them, so a retry of compensate() can still finish the
    # rollback rather than losing track of what was never (or only
    # partially) undone.
    fake_prov.clear_compensations.assert_not_called()
    fake_prov._state.delete.assert_not_called()


def test_compensate_credential_cleanup_failure_swallowed(tmp_path: Path) -> None:
    cred_store = MagicMock()
    cred_store.delete_credentials.side_effect = RuntimeError("cred io")

    orch = ProvisioningOrchestrator(
        credential_store=cred_store,
        environment_store=EnvironmentStore(storage_dir=tmp_path / "envs"),
        tool_agents={"docker_provisioner": MagicMock()},
    )
    orch.compensate("a1", [])


def test_compensate_environment_cleanup_failure_swallowed(tmp_path: Path, monkeypatch) -> None:
    from agent_team_studio.agent_provisioning_team import orchestrator as orch_mod

    monkeypatch.setattr(orch_mod, "cleanup_setup", MagicMock(side_effect=RuntimeError("env io")))
    orch = ProvisioningOrchestrator(
        environment_store=EnvironmentStore(storage_dir=tmp_path / "envs"),
        tool_agents={"docker_provisioner": MagicMock()},
    )
    orch.compensate("a1", [])  # must not raise


def test_compensate_skips_reused_tool_result(tmp_path: Path) -> None:
    """A reused (not this attempt's own) account must not be rolled back."""
    fake_prov = MagicMock()

    orch = ProvisioningOrchestrator(
        environment_store=EnvironmentStore(storage_dir=tmp_path / "envs"),
        tool_agents={"x": fake_prov, "docker_provisioner": MagicMock()},
    )

    reused = ToolProvisionResult(
        tool_name="t",
        success=True,
        provisioner_key="x",
        details={"reused": True},
    )
    orch.compensate("a1", [reused])
    fake_prov.list_compensations.assert_not_called()
    fake_prov.deprovision.assert_not_called()


def test_compensate_skips_credential_purge_for_reused_tool(tmp_path: Path) -> None:
    """A reused tool's credential entry must survive compensation untouched."""
    cred_store = MagicMock()

    orch = ProvisioningOrchestrator(
        credential_store=cred_store,
        environment_store=EnvironmentStore(storage_dir=tmp_path / "envs"),
        tool_agents={"x": MagicMock(), "docker_provisioner": MagicMock()},
    )

    reused = ToolProvisionResult(
        tool_name="t",
        success=True,
        provisioner_key="x",
        details={"reused": True},
    )
    orch.compensate("a1", [reused], tear_down_environment=False)
    cred_store.delete_tool_credentials.assert_not_called()


def test_compensate_purges_credentials_for_rolled_back_tool(tmp_path: Path) -> None:
    """A genuinely rolled-back tool's now-stale credential entry is purged."""
    cred_store = MagicMock()
    fake_prov = MagicMock()
    fake_prov.list_compensations.return_value = []
    fake_prov.deprovision.return_value = DeprovisionResult(tool_name="t", success=True)

    orch = ProvisioningOrchestrator(
        credential_store=cred_store,
        environment_store=EnvironmentStore(storage_dir=tmp_path / "envs"),
        tool_agents={"x": fake_prov, "docker_provisioner": MagicMock()},
    )

    fresh = ToolProvisionResult(tool_name="t", success=True, provisioner_key="x")
    # tear_down_environment=False: the whole-agent credential file is NOT
    # wiped, so the per-tool purge below must run independently of that flag.
    orch.compensate("a1", [fresh], tear_down_environment=False)
    fake_prov.deprovision.assert_called_once_with("a1", fencing_token=None)
    cred_store.delete_tool_credentials.assert_called_once_with("a1", "t", fencing_token=None)


def test_compensate_preserves_credentials_when_deprovision_reports_failure(
    tmp_path: Path,
) -> None:
    """A reported (not raised) deprovision failure must not purge the credential.

    Provisioner deprovision() methods commonly report failure via
    DeprovisionResult(success=False) rather than raising — "didn't raise" is
    not the same as "actually tore the account down". Purging the credential
    entry anyway would strip the only remaining way to reach an account that
    may still be live.
    """
    cred_store = MagicMock()
    fake_prov = MagicMock()
    fake_prov.list_compensations.return_value = []
    fake_prov.deprovision.return_value = DeprovisionResult(
        tool_name="t", success=False, error="daemon down"
    )

    orch = ProvisioningOrchestrator(
        credential_store=cred_store,
        environment_store=EnvironmentStore(storage_dir=tmp_path / "envs"),
        tool_agents={"x": fake_prov, "docker_provisioner": MagicMock()},
    )

    fresh = ToolProvisionResult(tool_name="t", success=True, provisioner_key="x")
    orch.compensate("a1", [fresh], tear_down_environment=False)
    fake_prov.deprovision.assert_called_once_with("a1", fencing_token=None)
    cred_store.delete_tool_credentials.assert_not_called()


def test_compensate_environment_teardown_preserves_failed_rollback_credentials(
    tmp_path: Path,
) -> None:
    """tear_down_environment=True must not defeat the per-tool preservation above.

    Before this fix, the environment-teardown path unconditionally called
    delete_credentials(agent_id) — wiping the WHOLE credential file,
    including entries the per-tool loop had just deliberately preserved
    because their rollback never confirmed success. A tool's account is
    frequently an external resource (e.g. a database) that destroying the
    container alone would not also destroy, so that credential may be the
    only remaining way to reach a still-live account.
    """
    from agent_team_studio.agent_provisioning_team.shared.credential_store import CredentialStore

    cred_store = CredentialStore(storage_dir=tmp_path / "creds")
    cred_store.store_credentials("a1", "ok", {"password": "p1"})
    cred_store.store_credentials("a1", "broken", {"password": "p2"})

    ok_prov = MagicMock()
    ok_prov.list_compensations.return_value = []
    ok_prov.deprovision.return_value = DeprovisionResult(tool_name="ok", success=True)

    broken_prov = MagicMock()
    broken_prov.list_compensations.return_value = []
    broken_prov.deprovision.return_value = DeprovisionResult(
        tool_name="broken", success=False, error="daemon down"
    )

    orch = ProvisioningOrchestrator(
        credential_store=cred_store,
        environment_store=EnvironmentStore(storage_dir=tmp_path / "envs"),
        tool_agents={
            "ok_provisioner": ok_prov,
            "broken_provisioner": broken_prov,
            "docker_provisioner": MagicMock(),
        },
    )

    results = [
        ToolProvisionResult(tool_name="ok", success=True, provisioner_key="ok_provisioner"),
        ToolProvisionResult(tool_name="broken", success=True, provisioner_key="broken_provisioner"),
    ]
    orch.compensate("a1", results, tear_down_environment=True)

    assert cred_store.get_credentials("a1", "ok") is None
    assert cred_store.get_credentials("a1", "broken") == {"password": "p2"}


def test_compensate_environment_teardown_deletes_whole_file_when_nothing_preserved(
    tmp_path: Path,
) -> None:
    """The simple whole-file delete is still used when every tool rolled back cleanly."""
    from agent_team_studio.agent_provisioning_team.shared.credential_store import CredentialStore

    cred_store = CredentialStore(storage_dir=tmp_path / "creds")
    cred_store.store_credentials("a1", "ok", {"password": "p1"})

    ok_prov = MagicMock()
    ok_prov.list_compensations.return_value = []
    ok_prov.deprovision.return_value = DeprovisionResult(tool_name="ok", success=True)

    orch = ProvisioningOrchestrator(
        credential_store=cred_store,
        environment_store=EnvironmentStore(storage_dir=tmp_path / "envs"),
        tool_agents={"ok_provisioner": ok_prov, "docker_provisioner": MagicMock()},
    )

    results = [ToolProvisionResult(tool_name="ok", success=True, provisioner_key="ok_provisioner")]
    orch.compensate("a1", results, tear_down_environment=True)

    assert cred_store.get_credentials("a1") is None


def test_compensate_environment_teardown_purges_legacy_only_tool_not_in_primary(
    tmp_path: Path, monkeypatch
) -> None:
    """The selective-preserve purge must reach a tool that ONLY exists in a legacy file.

    Before this fix, the purge loop enumerated stored tools via
    get_credentials(agent_id), which — like _read_agent_credentials — stops
    at the first candidate path that exists (primary, here). A tool whose
    credential was never migrated to primary and lives ONLY in a legacy
    file was never even enumerated, so delete_tool_credentials was never
    called for it and its stale secret survived compensation untouched.
    """
    from agent_team_studio.agent_provisioning_team.shared.credential_store import CredentialStore

    monkeypatch.chdir(tmp_path)
    key = CredentialStore.generate_key()
    cred_store = CredentialStore(storage_dir=tmp_path / "creds", encryption_key=key)
    cred_store.store_credentials("a1", "kept", {"password": "p1"})

    legacy_dir = tmp_path / ".agent_cache" / "provisioning_credentials"
    legacy_dir.mkdir(parents=True)
    legacy_store = CredentialStore(storage_dir=legacy_dir, encryption_key=key)
    legacy_store.store_credentials("a1", "stale", {"password": "p2"})

    orch = ProvisioningOrchestrator(
        credential_store=cred_store,
        environment_store=EnvironmentStore(storage_dir=tmp_path / "envs"),
        tool_agents={"docker_provisioner": MagicMock()},
    )

    # "kept" is preserved (reused=True); "stale" is never in tool_results at
    # all — e.g. a leftover credential unrelated to this attempt — so it must
    # still be purged by the environment-teardown pass since it isn't
    # deliberately preserved.
    results = [
        ToolProvisionResult(
            tool_name="kept", success=True, provisioner_key="x", details={"reused": True}
        )
    ]
    orch.compensate("a1", results, tear_down_environment=True)

    assert cred_store.get_credentials("a1", "kept") == {"password": "p1"}
    assert cred_store.get_credentials("a1", "stale") is None


def test_compensate_preserves_credentials_when_replay_step_fails(tmp_path: Path) -> None:
    """A failed (but swallowed) replay-compensation step must not purge the credential."""
    from agent_team_studio.agent_provisioning_team.shared.provisioner_state import (
        CompensationRecord,
    )

    cred_store = MagicMock()
    fake_prov = MagicMock()
    fake_prov.list_compensations.return_value = [CompensationRecord(kind="k1", payload={})]
    fake_prov.replay_compensation.side_effect = RuntimeError("replay boom")

    orch = ProvisioningOrchestrator(
        credential_store=cred_store,
        environment_store=EnvironmentStore(storage_dir=tmp_path / "envs"),
        tool_agents={"x": fake_prov, "docker_provisioner": MagicMock()},
    )

    fresh = ToolProvisionResult(tool_name="t", success=True, provisioner_key="x")
    orch.compensate("a1", [fresh], tear_down_environment=False)
    cred_store.delete_tool_credentials.assert_not_called()


def test_compensate_post_replay_state_cleanup_failure(tmp_path: Path) -> None:
    """If clear_compensations or _state.delete fails, swallow and move on."""
    from agent_team_studio.agent_provisioning_team.shared.provisioner_state import (
        CompensationRecord,
    )

    fake_prov = MagicMock()
    fake_prov.list_compensations.return_value = [CompensationRecord(kind="k1", payload={})]
    fake_prov.clear_compensations.side_effect = RuntimeError("clear io")

    orch = ProvisioningOrchestrator(
        environment_store=EnvironmentStore(storage_dir=tmp_path / "envs"),
        tool_agents={"x": fake_prov, "docker_provisioner": MagicMock()},
    )
    success = ToolProvisionResult(tool_name="t", success=True, provisioner_key="x")
    orch.compensate("a1", [success])


def test_compensate_skips_resource_with_stale_fencing_token(tmp_path: Path) -> None:
    """The preflight check must run BEFORE replay_compensation -- a real,
    destructive SQL/API call with no fencing check of its own -- not just
    before the final clear_compensations()/_state.delete()."""
    from agent_team_studio.agent_provisioning_team.shared.fencing import StaleFencingTokenError
    from agent_team_studio.agent_provisioning_team.shared.provisioner_state import (
        CompensationRecord,
    )

    fake_prov = MagicMock()
    fake_prov._state.check_fencing_token.side_effect = StaleFencingTokenError("a1", "x", 4, 5)
    fake_prov.list_compensations.return_value = [CompensationRecord(kind="k1", payload={})]

    orch = ProvisioningOrchestrator(
        environment_store=EnvironmentStore(storage_dir=tmp_path / "envs"),
        tool_agents={"x": fake_prov, "docker_provisioner": MagicMock()},
    )
    success = ToolProvisionResult(tool_name="t", success=True, provisioner_key="x")
    orch.compensate("a1", [success], fencing_token=4)

    fake_prov.replay_compensation.assert_not_called()
    fake_prov.deprovision.assert_not_called()


def test_compensate_preserves_credential_of_stale_skipped_tool(tmp_path: Path) -> None:
    """A tool skipped as stale must have its credential PRESERVED, not purged.

    Skipping the rollback means a newer owner already reclaimed the resource and
    its account may still be live; the tail credential cleanup must therefore
    not delete that tool's credential (the only remaining way to reach the
    account). The stale-skip must add the tool to preserved_credentials, exactly
    like every other skip path (no provisioner / reused)."""
    from agent_team_studio.agent_provisioning_team.shared.fencing import StaleFencingTokenError

    fake_prov = MagicMock()
    fake_prov._state.check_fencing_token.side_effect = StaleFencingTokenError("a1", "x", 4, 5)
    cred_store = MagicMock()
    cred_store.list_tool_names.return_value = {"t"}

    orch = ProvisioningOrchestrator(
        environment_store=EnvironmentStore(storage_dir=tmp_path / "envs"),
        credential_store=cred_store,
        tool_agents={"x": fake_prov, "docker_provisioner": MagicMock()},
    )
    success = ToolProvisionResult(tool_name="t", success=True, provisioner_key="x")
    orch.compensate("a1", [success], fencing_token=4)

    # The stale-skipped tool "t" is preserved: neither the blanket
    # delete_credentials nor a per-tool delete for "t" runs.
    cred_store.delete_credentials.assert_not_called()
    for purge_call in cred_store.delete_tool_credentials.call_args_list:
        assert purge_call.args[1] != "t", "stale-skipped tool's credential must be preserved"


def test_compensate_threads_fencing_token_through_replay_and_deprovision_paths(
    tmp_path: Path,
) -> None:
    from agent_team_studio.agent_provisioning_team.shared.provisioner_state import (
        CompensationRecord,
    )

    replaying = MagicMock()
    replaying.list_compensations.return_value = [CompensationRecord(kind="k1", payload={})]
    legacy = MagicMock()
    legacy.list_compensations.return_value = []
    docker = MagicMock()

    orch = ProvisioningOrchestrator(
        credential_store=MagicMock(),
        environment_store=EnvironmentStore(storage_dir=tmp_path / "envs"),
        tool_agents={"replaying": replaying, "legacy": legacy, "docker_provisioner": docker},
    )
    tool_results = [
        ToolProvisionResult(tool_name="a", success=True, provisioner_key="replaying"),
        ToolProvisionResult(tool_name="b", success=True, provisioner_key="legacy"),
    ]
    orch.compensate("a1", tool_results, fencing_token=7)

    replaying._state.check_fencing_token.assert_called_once_with("a1", 7)
    replaying.clear_compensations.assert_called_once_with("a1", fencing_token=7)
    replaying._state.delete.assert_called_once_with("a1", fencing_token=7)
    legacy.deprovision.assert_called_once_with("a1", fencing_token=7)
    docker.deprovision.assert_called_once_with("a1", fencing_token=7)
    orch.credential_store.delete_credentials.assert_called_once_with("a1", fencing_token=7)


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


def test_deprovision_threads_fencing_token(tmp_path: Path) -> None:
    fake_pg = MagicMock()
    fake_pg.deprovision.return_value = DeprovisionResult(tool_name="pg", success=True)
    fake_docker = MagicMock()
    fake_docker.deprovision.return_value = DeprovisionResult(tool_name="docker", success=True)
    cred_store = MagicMock()
    cred_store.delete_credentials.return_value = True

    orch = ProvisioningOrchestrator(
        credential_store=cred_store,
        environment_store=EnvironmentStore(storage_dir=tmp_path / "envs"),
        tool_agents={"postgres_provisioner": fake_pg, "docker_provisioner": fake_docker},
    )

    orch.deprovision("a1", fencing_token=9)

    fake_pg.deprovision.assert_called_once_with("a1", fencing_token=9)
    # docker_provisioner is reachable both via deprovision_tools()'s generic
    # loop over self.tool_agents and via the explicit docker-teardown call
    # below it (pre-existing, unrelated to fencing) -- assert every call it
    # got still carried the token, rather than asserting an exact count.
    assert all(c == call("a1", fencing_token=9) for c in fake_docker.deprovision.call_args_list)
    cred_store.delete_credentials.assert_called_once_with("a1", fencing_token=9)


def test_deprovision_propagates_stale_fencing_token_from_docker(tmp_path: Path) -> None:
    """Unlike compensate(), deprovision()'s docker/credential/environment
    calls are NOT wrapped in try/except (this predates fencing tokens) --
    a stale-token rejection propagates exactly as any other exception
    already would, giving the Temporal activity boundary a genuine,
    non-retryable failure to work with."""
    from agent_team_studio.agent_provisioning_team.shared.fencing import StaleFencingTokenError

    fake_docker = MagicMock()
    fake_docker.deprovision.side_effect = StaleFencingTokenError("a1", "docker_provisioner", 4, 5)

    orch = ProvisioningOrchestrator(
        credential_store=MagicMock(),
        environment_store=EnvironmentStore(storage_dir=tmp_path / "envs"),
        tool_agents={"docker_provisioner": fake_docker},
    )

    with pytest.raises(StaleFencingTokenError):
        orch.deprovision("a1", fencing_token=4)


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


def test_deprovision_stops_at_checkpoint_during_tool_loop(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.models import DeprovisionCancelledError

    fake_pg = MagicMock()
    fake_pg.deprovision.return_value = DeprovisionResult(tool_name="pg", success=True)
    fake_docker = MagicMock()
    fake_docker.deprovision.return_value = DeprovisionResult(tool_name="docker", success=True)

    cred_store = MagicMock()
    env_store = MagicMock()

    orch = ProvisioningOrchestrator(
        credential_store=cred_store,
        environment_store=env_store,
        tool_agents={"postgres_provisioner": fake_pg, "docker_provisioner": fake_docker},
    )

    with pytest.raises(DeprovisionCancelledError):
        orch.deprovision("a1", cancellation_checkpoint=lambda: True)

    fake_pg.deprovision.assert_not_called()
    fake_docker.deprovision.assert_not_called()
    cred_store.delete_credentials.assert_not_called()
    env_store.remove.assert_not_called()


def test_deprovision_stops_at_checkpoint_before_explicit_docker_call(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.models import DeprovisionCancelledError

    fake_docker = MagicMock()
    fake_docker.deprovision.return_value = DeprovisionResult(tool_name="docker", success=True)

    cred_store = MagicMock()
    env_store = MagicMock()

    orch = ProvisioningOrchestrator(
        credential_store=cred_store,
        environment_store=env_store,
        tool_agents={"docker_provisioner": fake_docker},
    )

    # Cancel on the 2nd checkpoint: the loop's single provisioner call passes
    # (checkpoint #1), cancellation fires before the explicit docker call.
    calls = {"n": 0}

    def checkpoint() -> bool:
        calls["n"] += 1
        return calls["n"] > 1

    with pytest.raises(DeprovisionCancelledError) as exc_info:
        orch.deprovision("a1", cancellation_checkpoint=checkpoint)

    fake_docker.deprovision.assert_called_once_with("a1", fencing_token=None)
    cred_store.delete_credentials.assert_not_called()
    env_store.remove.assert_not_called()
    assert exc_info.value.completed["tools"] == {"docker_provisioner": True}


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
    """If shutdown event flips between phases, compensate runs + raise fires."""
    from agent_team_studio.agent_provisioning_team import orchestrator as orch_mod

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
    from agent_team_studio.agent_provisioning_team import orchestrator as orch_mod

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


def test_no_legacy_v2_or_thread_fallback_symbols() -> None:
    """Hard cutover: no V2 workflow type, v2 activities, or thread fallback knob."""
    import agent_team_studio.agent_provisioning_team

    root = Path(agent_team_studio.agent_provisioning_team.__file__).resolve().parent
    # Plain literals are fine — this file lives under tests/, which is excluded.
    forbidden = (
        "AgentProvisioningWorkflowV2",
        "_activity_v2",
        "PROVISION_THREAD_FALLBACK",
    )
    hits: list[str] = []
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts or "tests" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in text:
                hits.append(f"{path.relative_to(root)}:{token}")
    assert hits == [], f"legacy cutover symbols still present: {hits}"
