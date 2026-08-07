"""End-to-end regression test for AgentLockStore fencing tokens.

Demonstrates the scenario fencing tokens exist to prevent: a workflow
acquires the agent_id lock, its worker goes silent long enough for the
lease to expire, a second workflow legitimately reclaims the lock (minting
a new fencing token), and the first (stale) workflow's subsequent mutation
attempts -- using its now-stale, previously-captured token -- are rejected
rather than applied. Driven directly through AgentLockStore and the real
store/orchestrator classes (no live Temporal needed), matching this team's
existing unit-test style: explicit `now=` timestamps, no real sleeps.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from agent_team_studio.agent_provisioning_team.models import DeprovisionResult, ToolProvisionResult
from agent_team_studio.agent_provisioning_team.orchestrator import ProvisioningOrchestrator
from agent_team_studio.agent_provisioning_team.shared.agent_lock import AgentLockStore
from agent_team_studio.agent_provisioning_team.shared.credential_store import CredentialStore
from agent_team_studio.agent_provisioning_team.shared.environment_store import (
    EnvironmentInfo,
    EnvironmentStore,
)
from agent_team_studio.agent_provisioning_team.shared.fencing import StaleFencingTokenError
from agent_team_studio.agent_provisioning_team.shared.provisioner_state import (
    CompensationRecord,
    ProvisionerStateStore,
)
from agent_team_studio.agent_provisioning_team.tool_agents.base import BaseToolProvisioner


def _acquire_then_reclaim(tmp_path: Path) -> Tuple[AgentLockStore, int, int]:
    """Acquire as owner A, expire the lease, reclaim as owner B.

    Returns (lock_store, stale_token_from_A, current_token_from_B).
    """
    lock_store = AgentLockStore(storage_dir=tmp_path / "locks", ttl_seconds=100)
    stale_token = lock_store.acquire("agent-1", owner="job-A", now=1000.0)
    # A's worker goes silent past its lease -- no renewal call happens at all.
    current_token = lock_store.acquire("agent-1", owner="job-B", now=1200.0)
    assert current_token > stale_token, "reclaim must mint a strictly higher token"
    return lock_store, stale_token, current_token


class _RecordingProvisioner(BaseToolProvisioner):
    """Minimal real provisioner: uses a genuine ProvisionerStateStore (not
    mocked) so the fencing checks under test are the real implementation,
    not a stand-in. Tracks whether its "real infrastructure" side effect
    (create/teardown) actually ran, to prove a rejected call never reaches it."""

    tool_name = "recording"

    def __init__(self, storage_dir: Path) -> None:
        self._state = ProvisionerStateStore("recording_provisioner", storage_dir=storage_dir)
        self.real_side_effects: List[str] = []

    def provision(self, agent_id, config, credentials, fencing_token=None) -> ToolProvisionResult:
        return self.run_idempotent(
            agent_id,
            credentials=credentials,
            create=lambda register: self._do_provision(agent_id, register),
            fencing_token=fencing_token,
        )

    def _do_provision(self, agent_id: str, register) -> Tuple[List[str], Dict[str, Any]]:
        self.real_side_effects.append(f"create:{agent_id}")
        register("recording.teardown", {"agent_id": agent_id})
        return ["read"], {"agent_id": agent_id}

    def verify_access(self, agent_id):
        return self._make_verification(passed=True, actual_permissions=[])

    def deprovision(self, agent_id: str, fencing_token=None) -> DeprovisionResult:
        if fencing_token is not None:
            self._state.check_fencing_token(agent_id, fencing_token)
        self.real_side_effects.append(f"deprovision:{agent_id}")
        self._state.delete(agent_id, fencing_token=fencing_token)
        return DeprovisionResult(tool_name=self.tool_name, success=True)

    def replay_compensation(self, agent_id, kind, payload) -> None:
        self.real_side_effects.append(f"replay:{kind}:{agent_id}")


def test_environment_store_rejects_stale_owners_mutation_after_reclaim(tmp_path: Path) -> None:
    _lock_store, stale_token, current_token = _acquire_then_reclaim(tmp_path)

    env_store = EnvironmentStore(storage_dir=tmp_path / "envs")
    # B (the new legitimate owner) sets up the environment using its fresh token.
    env_store.register(
        EnvironmentInfo(
            agent_id="agent-1", container_id="c1", container_name="c1", workspace_path="/w"
        ),
        fencing_token=current_token,
    )

    # A resumes, unaware its lease was reclaimed, and tries to mutate using
    # the token it captured before going silent.
    with pytest.raises(StaleFencingTokenError):
        env_store.update_status("agent-1", "ready", fencing_token=stale_token)

    # B's state is untouched by A's rejected write.
    assert env_store.get("agent-1").status == "running"


def test_credential_store_rejects_stale_owners_mutation_after_reclaim(tmp_path: Path) -> None:
    _lock_store, stale_token, current_token = _acquire_then_reclaim(tmp_path)

    cred_store = CredentialStore(storage_dir=tmp_path / "creds")
    cred_store.store_credentials(
        "agent-1", "postgresql", {"password": "b-owns-this"}, fencing_token=current_token
    )

    with pytest.raises(StaleFencingTokenError):
        cred_store.store_credentials(
            "agent-1", "postgresql", {"password": "a-tries-to-overwrite"}, fencing_token=stale_token
        )

    assert cred_store.get_credentials("agent-1", "postgresql") == {"password": "b-owns-this"}


def test_provisioner_state_store_rejects_stale_owners_mutation_after_reclaim(
    tmp_path: Path,
) -> None:
    _lock_store, stale_token, current_token = _acquire_then_reclaim(tmp_path)

    state = ProvisionerStateStore("docker_provisioner", storage_dir=tmp_path / "state")
    state.put("agent-1", {"container_name": "b-owns-this"}, fencing_token=current_token)

    with pytest.raises(StaleFencingTokenError):
        state.put("agent-1", {"container_name": "a-tries-to-overwrite"}, fencing_token=stale_token)
    with pytest.raises(StaleFencingTokenError):
        state.delete("agent-1", fencing_token=stale_token)
    with pytest.raises(StaleFencingTokenError):
        state.add_compensation(
            "agent-1", CompensationRecord(kind="k", payload={}), fencing_token=stale_token
        )

    assert state.get("agent-1") == {"container_name": "b-owns-this"}


def test_provisioner_preflight_rejects_stale_token_before_any_real_side_effect(
    tmp_path: Path,
) -> None:
    """The core safety property: a stale caller's real infrastructure
    mutation must never run at all -- not just have its bookkeeping
    write rejected after the fact."""
    from agent_team_studio.agent_provisioning_team.models import GeneratedCredentials

    _lock_store, stale_token, current_token = _acquire_then_reclaim(tmp_path)

    prov = _RecordingProvisioner(tmp_path / "state")
    # B legitimately provisions first.
    result = prov.provision(
        "agent-1", {}, GeneratedCredentials(tool_name="recording"), fencing_token=current_token
    )
    assert result.success is True
    assert prov.real_side_effects == ["create:agent-1"]

    # A resumes and tries to deprovision using its stale token -- this must
    # be rejected BEFORE the "real" teardown side effect runs.
    with pytest.raises(StaleFencingTokenError):
        prov.deprovision("agent-1", fencing_token=stale_token)
    assert prov.real_side_effects == ["create:agent-1"], "no new side effect from the rejected call"

    # B's resource is still intact and can still be legitimately torn down.
    prov.deprovision("agent-1", fencing_token=current_token)
    assert prov.real_side_effects == ["create:agent-1", "deprovision:agent-1"]


def test_orchestrator_compensate_rejects_stale_token_before_replaying(tmp_path: Path) -> None:
    """ProvisioningOrchestrator.compensate() must reject a stale caller's
    rollback attempt BEFORE replaying any persisted compensation record --
    replay_compensation executes real destructive SQL/API calls with no
    fencing check of its own."""
    _lock_store, stale_token, current_token = _acquire_then_reclaim(tmp_path)

    prov = _RecordingProvisioner(tmp_path / "state")
    # B legitimately provisions and registers a compensation record.
    prov._state.put("agent-1", {"agent_id": "agent-1"}, fencing_token=current_token)
    prov._state.add_compensation(
        "agent-1",
        CompensationRecord(kind="recording.teardown", payload={}),
        fencing_token=current_token,
    )

    orch = ProvisioningOrchestrator(
        credential_store=CredentialStore(storage_dir=tmp_path / "creds"),
        environment_store=EnvironmentStore(storage_dir=tmp_path / "envs"),
        tool_agents={
            "recording_provisioner": prov,
            "docker_provisioner": _RecordingProvisioner(tmp_path / "docker_state"),
        },
    )
    tool_results = [
        ToolProvisionResult(
            tool_name="recording", success=True, provisioner_key="recording_provisioner"
        )
    ]

    # A resumes and tries to compensate (roll back) using its stale token.
    orch.compensate("agent-1", tool_results, fencing_token=stale_token)

    # The rejection must have happened before replay_compensation ran.
    assert prov.real_side_effects == [], "compensate must not replay with a stale token"
    # B's state survives A's rejected rollback attempt.
    assert prov._state.get("agent-1") == {"agent_id": "agent-1"}


def test_orchestrator_deprovision_rejects_stale_token(tmp_path: Path) -> None:
    """ProvisioningOrchestrator.deprovision() -- the primary deliverable of
    AgentDeprovisioningWorkflow's deprovision_activity -- must reject a
    stale-token caller's teardown attempt."""
    _lock_store, stale_token, current_token = _acquire_then_reclaim(tmp_path)

    docker = _RecordingProvisioner(tmp_path / "docker_state")
    docker._state.put("agent-1", {"container_name": "b-owns-this"}, fencing_token=current_token)

    cred_store = CredentialStore(storage_dir=tmp_path / "creds")
    cred_store.store_credentials(
        "agent-1", "postgresql", {"password": "b-owns-this"}, fencing_token=current_token
    )

    orch = ProvisioningOrchestrator(
        credential_store=cred_store,
        environment_store=EnvironmentStore(storage_dir=tmp_path / "envs"),
        tool_agents={"docker_provisioner": docker},
    )

    # A resumes and tries to deprovision using its stale token. Unlike
    # compensate(), deprovision()'s docker/credential/environment calls are
    # not wrapped in try/except (pre-existing, unrelated to fencing), so the
    # rejection propagates instead of being folded into a soft failure --
    # giving the Temporal activity boundary a genuine, non-retryable error.
    with pytest.raises(StaleFencingTokenError):
        orch.deprovision("agent-1", fencing_token=stale_token)

    assert docker.real_side_effects == [], "no real teardown ran for the stale-token caller"
    assert cred_store.get_credentials("agent-1", "postgresql") == {"password": "b-owns-this"}

    # B's own deprovision, using the current token, still works. (docker is
    # reachable both via deprovision_tools()'s generic loop over
    # self.tool_agents and via the explicit docker-teardown call below it --
    # pre-existing, unrelated to fencing -- so it legitimately tears down
    # twice; both calls carry the correct current_token either way.)
    orch.deprovision("agent-1", fencing_token=current_token)
    assert docker.real_side_effects == ["deprovision:agent-1", "deprovision:agent-1"]
    assert cred_store.get_credentials("agent-1") is None


def test_credential_store_tombstone_survives_delete_and_rejects_stale_bootstrap_write(
    tmp_path: Path,
) -> None:
    """delete_credentials() must not simply vanish the file: doing so would
    reset a later caller's prior-token lookup to a bootstrap
    current_token=None, letting a stale caller's store_credentials call
    silently resurrect a secret after a legitimate newer owner tore it
    down."""
    _lock_store, stale_token, current_token = _acquire_then_reclaim(tmp_path)

    cred_store = CredentialStore(storage_dir=tmp_path / "creds")
    cred_store.store_credentials(
        "agent-1", "postgresql", {"password": "b-owns-this"}, fencing_token=current_token
    )
    # B legitimately tears down.
    cred_store.delete_credentials("agent-1", fencing_token=current_token)
    assert cred_store.get_credentials("agent-1") is None

    # A resumes and tries to write using its stale, pre-reclaim token -- this
    # must still be rejected, not silently accepted as a "bootstrap" write
    # just because the file is gone.
    with pytest.raises(StaleFencingTokenError):
        cred_store.store_credentials(
            "agent-1", "postgresql", {"password": "a-resurrects-this"}, fencing_token=stale_token
        )
    assert cred_store.get_credentials("agent-1") is None


def test_environment_store_tombstone_survives_remove_and_rejects_stale_bootstrap_write(
    tmp_path: Path,
) -> None:
    """remove() must not simply vanish the record: doing so would reset a
    later caller's prior-token lookup to a bootstrap current_token=None,
    letting a stale caller's register() call silently recreate an
    environment after a legitimate newer owner tore it down."""
    _lock_store, stale_token, current_token = _acquire_then_reclaim(tmp_path)

    env_store = EnvironmentStore(storage_dir=tmp_path / "envs")
    env_store.register(
        EnvironmentInfo(
            agent_id="agent-1", container_id="c1", container_name="c1", workspace_path="/w"
        ),
        fencing_token=current_token,
    )
    env_store.remove("agent-1", fencing_token=current_token)
    assert env_store.get("agent-1") is None

    with pytest.raises(StaleFencingTokenError):
        env_store.register(
            EnvironmentInfo(
                agent_id="agent-1",
                container_id="c-resurrected",
                container_name="c1",
                workspace_path="/w",
            ),
            fencing_token=stale_token,
        )
    assert env_store.get("agent-1") is None


def test_provisioner_state_tombstone_survives_delete_and_rejects_stale_bootstrap_write(
    tmp_path: Path,
) -> None:
    """delete() must not remove the row entirely: doing so would reset a
    later caller's prior-token lookup to a bootstrap current_token=None,
    letting a stale caller's put() call silently resurrect state after a
    legitimate newer owner tore it down."""
    _lock_store, stale_token, current_token = _acquire_then_reclaim(tmp_path)

    state = ProvisionerStateStore("docker_provisioner", storage_dir=tmp_path / "state")
    state.put("agent-1", {"container_name": "b-owns-this"}, fencing_token=current_token)
    state.delete("agent-1", fencing_token=current_token)
    assert state.get("agent-1") is None

    with pytest.raises(StaleFencingTokenError):
        state.put("agent-1", {"container_name": "a-resurrects-this"}, fencing_token=stale_token)
    assert state.get("agent-1") is None


def test_run_idempotent_reuse_path_persists_bumped_token(tmp_path: Path) -> None:
    """A reuse-path call (existing state found, no new create) must still
    persist its fencing_token as the store's new high-water mark -- the
    short-circuit return never calls state.put() on its own, so without this
    fix the store's mark would stay wherever the ORIGINAL create left it,
    letting a caller presenting that original (now-superseded) token still
    pass a later check even after a legitimate newer owner touched this same
    state via nothing but a reuse."""
    from agent_team_studio.agent_provisioning_team.models import GeneratedCredentials

    lock_store = AgentLockStore(storage_dir=tmp_path / "locks", ttl_seconds=100)
    token_a = lock_store.acquire("agent-1", owner="job-A", now=1000.0)
    prov = _RecordingProvisioner(tmp_path / "state")
    # A creates the resource for real.
    prov.provision(
        "agent-1", {}, GeneratedCredentials(tool_name="recording"), fencing_token=token_a
    )
    assert prov.real_side_effects == ["create:agent-1"]

    # A's worker goes silent; B reclaims.
    token_b = lock_store.acquire("agent-1", owner="job-B", now=1200.0)
    assert token_b > token_a

    # B calls provision() again for the same agent_id -- state.get() finds
    # A's existing details, so this hits the reuse short-circuit, not create.
    result = prov.provision(
        "agent-1", {}, GeneratedCredentials(tool_name="recording"), fencing_token=token_b
    )
    assert result.success is True
    assert prov.real_side_effects == ["create:agent-1"], (
        "reuse must not re-run the real side effect"
    )

    # A resumes, unaware of the reclaim, and tries to deprovision using its
    # original (now-superseded) token. The reuse call above must have
    # advanced the store's high-water mark to token_b, or this would wrongly
    # be accepted (token_a would still equal the store's un-bumped mark).
    with pytest.raises(StaleFencingTokenError):
        prov.deprovision("agent-1", fencing_token=token_a)
    assert prov.real_side_effects == ["create:agent-1"], "no teardown ran for the stale caller"

    # B's own teardown, using the token it actually reused with, still works.
    prov.deprovision("agent-1", fencing_token=token_b)
    assert prov.real_side_effects == ["create:agent-1", "deprovision:agent-1"]
