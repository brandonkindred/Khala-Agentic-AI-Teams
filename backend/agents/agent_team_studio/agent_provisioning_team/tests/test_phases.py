"""Unit tests for each phase function.

These cover the procedural orchestration in `phases/` directly so we
don't have to drive a full workflow through the orchestrator to land
coverage on every branch.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from agent_team_studio.agent_provisioning_team.models import (
    AccessAuditResult,
    DeliverResult,
    EnvironmentInfo,
    GeneratedCredentials,
    OnboardingPacket,
    Phase,
    ProvisioningResult,
    ToolOnboardingInfo,
    ToolProvisionResult,
)

# ---------------------------------------------------------------------------
# setup phase
# ---------------------------------------------------------------------------


def test_run_setup_reuses_existing_running_environment(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.phases.setup import run_setup
    from agent_team_studio.agent_provisioning_team.shared.environment_store import (
        EnvironmentInfo as StoreEnvInfo,
    )
    from agent_team_studio.agent_provisioning_team.shared.environment_store import EnvironmentStore
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import ToolManifest

    env_store = EnvironmentStore(storage_dir=tmp_path / "envs")
    env_store.register(
        StoreEnvInfo(
            agent_id="a1",
            container_id="c1",
            container_name="agent-a1",
            ssh_host="localhost",
            ssh_port=22001,
            workspace_path="/workspace/a1",
            status="running",
        )
    )

    manifest = ToolManifest()
    docker = MagicMock()

    calls = []

    def cb(msg):
        calls.append(msg)

    result = run_setup(
        agent_id="a1",
        manifest=manifest,
        environment_store=env_store,
        docker_provisioner=docker,
        progress_callback=cb,
    )

    assert result.success is True
    assert result.environment.container_id == "c1"
    # Existing env path doesn't shell out to docker
    docker.provision.assert_not_called()


def test_run_setup_creates_new_container(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.phases.setup import run_setup
    from agent_team_studio.agent_provisioning_team.shared.environment_store import EnvironmentStore
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import ToolManifest

    env_store = EnvironmentStore(storage_dir=tmp_path / "envs")
    manifest = ToolManifest()

    docker = MagicMock()
    docker.provision.return_value = ToolProvisionResult(
        tool_name="docker",
        success=True,
        details={
            "container_id": "c-new",
            "container_name": "agent-a2",
            "ssh_port": 22002,
            "workspace_path": "/workspace/a2",
        },
    )

    result = run_setup(
        agent_id="a2",
        manifest=manifest,
        environment_store=env_store,
        docker_provisioner=docker,
        progress_callback=lambda msg: None,
    )

    assert result.success is True
    assert result.environment.container_id == "c-new"
    assert env_store.exists("a2")
    stored = env_store.get("a2")
    assert stored.updated_at == stored.created_at


def test_run_setup_threads_fencing_token(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.phases.setup import run_setup
    from agent_team_studio.agent_provisioning_team.shared.environment_store import EnvironmentStore
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import ToolManifest

    env_store = EnvironmentStore(storage_dir=tmp_path / "envs")
    manifest = ToolManifest()

    docker = MagicMock()
    docker.provision.return_value = ToolProvisionResult(
        tool_name="docker",
        success=True,
        details={
            "container_id": "c-new",
            "container_name": "agent-a2",
            "ssh_port": 22002,
            "workspace_path": "/workspace/a2",
        },
    )

    result = run_setup(
        agent_id="a2",
        manifest=manifest,
        environment_store=env_store,
        docker_provisioner=docker,
        fencing_token=5,
    )

    assert result.success is True
    assert docker.provision.call_args.kwargs["fencing_token"] == 5
    stored = env_store._read_env_data("a2")[0]
    assert stored["fencing_token"] == 5


def test_run_setup_passes_job_id_to_docker_provision(tmp_path: Path) -> None:
    """run_setup is the ONLY code path that creates the real agent_id
    environment container -- job_id must reach docker.provision's config
    here, or the khala.job_id label this feature depends on never gets
    stamped on the container check_existing_environment_activity /
    compensate_activity actually inspect.
    """
    from agent_team_studio.agent_provisioning_team.phases.setup import run_setup
    from agent_team_studio.agent_provisioning_team.shared.environment_store import EnvironmentStore
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import ToolManifest
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        JOB_ID_CONFIG_KEY,
    )

    env_store = EnvironmentStore(storage_dir=tmp_path / "envs")
    manifest = ToolManifest()

    docker = MagicMock()
    docker.provision.return_value = ToolProvisionResult(
        tool_name="docker",
        success=True,
        details={
            "container_id": "c-new",
            "container_name": "agent-a2",
            "ssh_port": 22002,
            "workspace_path": "/workspace/a2",
        },
    )

    run_setup(
        agent_id="a2",
        manifest=manifest,
        environment_store=env_store,
        docker_provisioner=docker,
        progress_callback=lambda msg: None,
        job_id="job-77",
    )

    _args, kwargs = docker.provision.call_args
    assert kwargs["config"][JOB_ID_CONFIG_KEY] == "job-77"


def test_run_setup_omits_job_id_from_docker_provision_when_not_given(tmp_path: Path) -> None:
    """job_id is optional -- omitting it must not inject anything, keeping
    non-Temporal callers behaving exactly as before this labeling primitive
    existed."""
    from agent_team_studio.agent_provisioning_team.phases.setup import run_setup
    from agent_team_studio.agent_provisioning_team.shared.environment_store import EnvironmentStore
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import ToolManifest
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        JOB_ID_CONFIG_KEY,
    )

    env_store = EnvironmentStore(storage_dir=tmp_path / "envs")
    manifest = ToolManifest()

    docker = MagicMock()
    docker.provision.return_value = ToolProvisionResult(
        tool_name="docker",
        success=True,
        details={
            "container_id": "c-new",
            "container_name": "agent-a2",
            "ssh_port": 22002,
            "workspace_path": "/workspace/a2",
        },
    )

    run_setup(
        agent_id="a2",
        manifest=manifest,
        environment_store=env_store,
        docker_provisioner=docker,
        progress_callback=lambda msg: None,
    )

    _args, kwargs = docker.provision.call_args
    assert JOB_ID_CONFIG_KEY not in kwargs["config"]


def test_run_setup_preserves_created_at_and_refreshes_updated_at_on_reregister(
    tmp_path: Path,
) -> None:
    """Re-registering a non-running environment keeps the original created_at
    and previously provisioned tools, but stamps updated_at with the current
    (replacement) time, not the stale created_at."""
    from agent_team_studio.agent_provisioning_team.phases.setup import run_setup
    from agent_team_studio.agent_provisioning_team.shared.environment_store import (
        EnvironmentInfo as StoreEnvInfo,
    )
    from agent_team_studio.agent_provisioning_team.shared.environment_store import EnvironmentStore
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import ToolManifest

    env_store = EnvironmentStore(storage_dir=tmp_path / "envs")
    env_store.register(
        StoreEnvInfo(
            agent_id="a3",
            container_id="c-old",
            container_name="agent-a3",
            workspace_path="/workspace/a3",
            status="stopped",
            tools_provisioned=["pg"],
            created_at="2020-01-01T00:00:00+00:00",
            updated_at="2020-01-01T00:00:00+00:00",
        )
    )

    manifest = ToolManifest()
    docker = MagicMock()
    docker.provision.return_value = ToolProvisionResult(
        tool_name="docker",
        success=True,
        details={
            "container_id": "c-new",
            "container_name": "agent-a3",
            "ssh_port": 22003,
            "workspace_path": "/workspace/a3",
        },
    )

    result = run_setup(
        agent_id="a3",
        manifest=manifest,
        environment_store=env_store,
        docker_provisioner=docker,
        progress_callback=lambda msg: None,
    )

    assert result.success is True
    stored = env_store.get("a3")
    assert stored.created_at == "2020-01-01T00:00:00+00:00"
    assert stored.updated_at != "2020-01-01T00:00:00+00:00"
    assert stored.tools_provisioned == ["pg"]


def test_run_setup_returns_failure_on_docker_error(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.phases.setup import run_setup
    from agent_team_studio.agent_provisioning_team.shared.environment_store import EnvironmentStore
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import ToolManifest

    env_store = EnvironmentStore(storage_dir=tmp_path / "envs")
    docker = MagicMock()
    docker.provision.return_value = ToolProvisionResult(
        tool_name="docker", success=False, error="docker daemon down"
    )

    result = run_setup(
        agent_id="a3",
        manifest=ToolManifest(),
        environment_store=env_store,
        docker_provisioner=docker,
    )

    assert result.success is False
    assert "docker daemon down" in result.error


def test_cleanup_setup_removes_record_before_deprovisioning(tmp_path: Path) -> None:
    """cleanup_setup clears the env record BEFORE deleting the container.

    The reverse order could strand a ``running`` record pointing at a deleted
    container, which a later ``run_setup`` fast path would return as success.
    """
    from agent_team_studio.agent_provisioning_team.phases.setup import cleanup_setup

    manager = MagicMock()
    docker = manager.docker
    env_store = manager.env_store

    result = cleanup_setup("a1", environment_store=env_store, docker_provisioner=docker)
    assert result is True
    docker.deprovision.assert_called_once_with("a1", fencing_token=None)
    env_store.remove.assert_called_once_with("a1", fencing_token=None)
    assert manager.mock_calls.index(
        ("env_store.remove", ("a1",), {"fencing_token": None})
    ) < manager.mock_calls.index(("docker.deprovision", ("a1",), {"fencing_token": None}))


def test_cleanup_setup_threads_fencing_token(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.phases.setup import cleanup_setup

    docker = MagicMock()
    env_store = MagicMock()

    cleanup_setup("a1", environment_store=env_store, docker_provisioner=docker, fencing_token=7)
    docker.deprovision.assert_called_once_with("a1", fencing_token=7)
    env_store.remove.assert_called_once_with("a1", fencing_token=7)


def test_cleanup_setup_reports_failed_teardown(tmp_path: Path, caplog) -> None:
    """A teardown that reports failure must surface as False, not silent success.

    The provisioner keeps its state row on a failed removal, so the surviving
    container stays reachable by agent id — but the caller must know cleanup
    was partial.
    """
    from agent_team_studio.agent_provisioning_team.models import DeprovisionResult
    from agent_team_studio.agent_provisioning_team.phases.setup import cleanup_setup

    docker = MagicMock()
    docker.deprovision.return_value = DeprovisionResult(
        tool_name="docker", success=False, error="docker rm failed: device busy"
    )
    env_store = MagicMock()

    with caplog.at_level(logging.ERROR):
        result = cleanup_setup("a1", environment_store=env_store, docker_provisioner=docker)

    assert result is False
    assert "device busy" in caplog.text


# Rollback-scenario helpers: every case arranges the same shape — a provision
# result, a (possibly failing) register, and an ownership state — so the
# factories keep each test down to its meaningful delta.


def _rollback_env_store(get=None, register_error=None):
    """MagicMock EnvironmentStore: ``get`` behavior plus a failing register."""
    env_store = MagicMock()
    if isinstance(get, list):
        env_store.get.side_effect = get
    else:
        env_store.get.return_value = get
    env_store.register.side_effect = register_error or RuntimeError("register boom")
    return env_store


def _docker_stub(**details):
    """MagicMock docker provisioner whose provision succeeds with ``details``."""
    docker = MagicMock()
    docker.provision.return_value = ToolProvisionResult(
        tool_name="docker", success=True, details=details
    )
    return docker


def _stored_env(agent_id, status="ready", container_id="c-existing", ssh_port=22004):
    from agent_team_studio.agent_provisioning_team.shared.environment_store import (
        EnvironmentInfo as StoreEnvInfo,
    )

    return StoreEnvInfo(
        agent_id=agent_id,
        container_id=container_id,
        container_name=f"agent-{agent_id}",
        ssh_host="localhost",
        ssh_port=ssh_port,
        workspace_path=f"/workspace/{agent_id}",
        status=status,
    )


def _run_setup_expecting(match, agent_id, env_store, docker, **kwargs):
    from agent_team_studio.agent_provisioning_team.phases.setup import run_setup
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import ToolManifest

    with pytest.raises(RuntimeError, match=match):
        run_setup(
            agent_id=agent_id,
            manifest=ToolManifest(),
            environment_store=env_store,
            docker_provisioner=docker,
            **kwargs,
        )


def test_run_setup_rolls_back_new_container_when_register_fails() -> None:
    """A container this attempt created is deprovisioned when register fails.

    Register is atomic, so the failed attempt left no record; nothing needs
    removing from the store.
    """
    env_store = _rollback_env_store(get=None)
    docker = _docker_stub(container_id="c-new", container_name="agent-a2")

    _run_setup_expecting("register boom", "a2", env_store, docker)

    docker.deprovision.assert_called_once_with("a2", fencing_token=None)
    env_store.remove.assert_not_called()


def test_run_setup_preserves_ready_agent_on_reused_container() -> None:
    """A reused container backed by a prior record (any status) is preserved.

    Re-provisioning a completed agent leaves a ``ready`` record; the reused
    container belongs to that prior setup, and atomic register means the failed
    attempt could not have corrupted that record — nothing is torn down or
    rewritten.
    """
    ready_env = _stored_env("a4")
    env_store = _rollback_env_store(get=ready_env)
    docker = _docker_stub(container_id="c-existing", container_name="agent-a4", reused=True)

    _run_setup_expecting("register boom", "a4", env_store, docker)

    docker.deprovision.assert_not_called()
    env_store.remove.assert_not_called()


def test_run_setup_preserves_concurrent_owner_on_reused_orphan() -> None:
    """A record appearing between pre-check and rollback marks a concurrent owner.

    reused=True with no pre-check record looks like an orphan, but the
    rollback's ownership read finds a record — this attempt's register is
    atomic and failed, so that record can only be a concurrent job's; its
    container must be preserved.
    """
    env_store = _rollback_env_store(get=[None, _stored_env("a5", status="running")])
    docker = _docker_stub(container_id="c-x", container_name="agent-a5", reused=True)

    _run_setup_expecting("register boom", "a5", env_store, docker)

    assert env_store.get.call_count == 2
    docker.deprovision.assert_not_called()


def test_run_setup_reclaims_reused_orphan_with_no_env_record() -> None:
    """A reused container with no record anywhere is a retry orphan: reclaim it."""
    env_store = _rollback_env_store(get=None)
    docker = _docker_stub(container_id="c-orphan", container_name="agent-a7", reused=True)

    _run_setup_expecting("register boom", "a7", env_store, docker)

    docker.deprovision.assert_called_once_with("a7", fencing_token=None)


def test_run_setup_preserves_reused_container_when_registry_unreadable() -> None:
    """A missing record is only proof of an orphan when the registry is readable.

    get() maps unreadable-store errors (e.g. EACCES) to None, so with the
    registry unreadable a healthy reused container would look like an orphan;
    the rollback must preserve it when ownership cannot be established.
    """
    env_store = _rollback_env_store(get=None)
    env_store.readable.return_value = False
    docker = _docker_stub(container_id="c-live", container_name="agent-a9", reused=True)

    _run_setup_expecting("register boom", "a9", env_store, docker)

    docker.deprovision.assert_not_called()


def test_run_setup_preserves_container_adopted_by_concurrent_job() -> None:
    """A created container that a concurrent job registered is theirs now.

    Job A creates the container; job B reuses and registers it before A's
    register fails. A's rollback sees a record it did not write (register is
    atomic, so A's failed write left nothing) and must not deprovision B's
    container.
    """
    adopted = _stored_env("a6", status="running", container_id="c-shared")
    env_store = _rollback_env_store(get=[None, adopted])
    docker = _docker_stub(container_id="c-shared", container_name="agent-a6")

    _run_setup_expecting("register boom", "a6", env_store, docker)

    docker.deprovision.assert_not_called()
    env_store.remove.assert_not_called()


def test_run_setup_reclaims_fresh_container_despite_unrelated_record_update() -> None:
    """An unrelated concurrent update to the OLD record must not fake adoption.

    Ownership must be decided by container identity, not by whether ANY field
    of the record changed: a concurrent add_tool/update_status touching the
    OLD (non-running) container's record — e.g. bumping updated_at — makes the
    whole-record comparison differ even though nobody adopted the container
    THIS attempt just created. That must still be reclaimed, not leaked.
    """
    prior = _stored_env("a14", status="stopped", container_id="c-old")
    updated_prior = _stored_env("a14", status="stopped", container_id="c-old")
    updated_prior.updated_at = "2030-01-01T00:00:00+00:00"  # unrelated field bump
    assert updated_prior.to_dict() != prior.to_dict()  # sanity: whole-dict differs

    env_store = _rollback_env_store(get=[prior, updated_prior])
    docker = _docker_stub(container_id="c-new", container_name="agent-a14")

    _run_setup_expecting("register boom", "a14", env_store, docker)

    docker.deprovision.assert_called_once_with("a14", fencing_token=None)


def test_run_setup_keeps_prior_record_when_fresh_create_rolls_back() -> None:
    """Reclaiming a fresh container leaves an untouched prior record in place.

    A prior non-running record (docker state lost, so provision created fresh)
    is not this attempt's to delete: the fresh container is torn down, but the
    record keeps its continuity (created_at / tools) and its non-running status
    cannot short-circuit a retry's fast path.
    """
    prior = _stored_env("a8", status="stopped", container_id="c-old")
    # Pre-check and rollback read the same, unchanged record.
    env_store = _rollback_env_store(get=prior)
    docker = _docker_stub(container_id="c-new", container_name="agent-a8")

    _run_setup_expecting("register boom", "a8", env_store, docker)

    docker.deprovision.assert_called_once_with("a8", fencing_token=None)
    env_store.remove.assert_not_called()


def test_run_setup_rolls_back_when_progress_callback_raises() -> None:
    """A progress callback that raises after provisioning triggers the rollback."""
    env_store = _rollback_env_store(get=None)
    docker = _docker_stub(container_id="c-new", container_name="agent-a12")

    def cb(msg):
        if "Registering" in msg:
            raise RuntimeError("callback boom")

    _run_setup_expecting("callback boom", "a12", env_store, docker, progress_callback=cb)

    docker.deprovision.assert_called_once_with("a12", fencing_token=None)


def test_run_setup_rolls_back_when_on_registered_raises(tmp_path: Path) -> None:
    """A failing on_registered hook rolls back this attempt's own record too.

    A durable checkpoint write (e.g. a Temporal activity's job-store record)
    belongs inside this same atomic boundary: if it fails or the activity is
    cancelled here, the container this attempt just created must not leak,
    exactly like a failing register call. Unlike a failing register() call,
    though, this attempt's own record now sits in the store with a
    container_id that matches `result` — the exact signal the "adopted by a
    concurrent job" heuristic uses for the register-failure case — so the
    rollback must recognize this as its own write (not a concurrent owner's)
    and remove it, using a REAL EnvironmentStore rather than an always-None
    stub so `get()` reflects what `register()` actually just wrote.
    """
    from agent_team_studio.agent_provisioning_team.shared.environment_store import EnvironmentStore

    env_store = EnvironmentStore(storage_dir=tmp_path)
    docker = _docker_stub(container_id="c-new", container_name="agent-a15")

    def on_registered(env_info):
        raise RuntimeError("checkpoint boom")

    _run_setup_expecting("checkpoint boom", "a15", env_store, docker, on_registered=on_registered)

    docker.deprovision.assert_called_once_with("a15", fencing_token=None)
    assert env_store.get("a15") is None


def test_run_setup_removes_overwritten_prior_record_when_on_registered_raises(
    tmp_path: Path,
) -> None:
    """Contrast with the register()-fails case: here register() overwrote a
    prior record before on_registered raised, so there is no stale prior
    content left to preserve for continuity — the rollback must remove this
    attempt's own (now current) record, not leave a dangling `running` row.
    """
    from agent_team_studio.agent_provisioning_team.shared.environment_store import (
        EnvironmentInfo as StoreEnvInfo,
    )
    from agent_team_studio.agent_provisioning_team.shared.environment_store import EnvironmentStore

    env_store = EnvironmentStore(storage_dir=tmp_path)
    env_store.register(
        StoreEnvInfo(
            agent_id="a19",
            container_id="c-old",
            container_name="agent-a19",
            status="stopped",
        )
    )
    docker = _docker_stub(container_id="c-new", container_name="agent-a19")

    def on_registered(env_info):
        raise RuntimeError("checkpoint boom")

    _run_setup_expecting("checkpoint boom", "a19", env_store, docker, on_registered=on_registered)

    docker.deprovision.assert_called_once_with("a19", fencing_token=None)
    assert env_store.get("a19") is None


def test_run_setup_preserves_container_when_record_removal_fails() -> None:
    """A record-removal failure during rollback must not fall through to teardown.

    If env_store.remove() itself raises (e.g. a now-read-only registry
    directory) after on_registered failed, the record this attempt just wrote
    survives (status="running") — deprovisioning the container anyway would
    leave that surviving record pointing at a container that's actually gone,
    which a later run_setup's fast path would trust as healthy. Skipping
    teardown here keeps record and container consistent with each other,
    matching cleanup_setup's own record-then-container ordering rule.
    """
    env_store = _rollback_env_store(get=None, register_error=lambda *a, **kw: None)
    env_store.remove.side_effect = OSError("registry directory is read-only")
    docker = _docker_stub(container_id="c-new", container_name="agent-a20")

    def on_registered(env_info):
        raise RuntimeError("checkpoint boom")

    _run_setup_expecting("checkpoint boom", "a20", env_store, docker, on_registered=on_registered)

    env_store.remove.assert_called_once_with("a20", fencing_token=None)
    docker.deprovision.assert_not_called()


def test_run_setup_preserves_reused_container_when_checkpoint_fails_after_reregister() -> None:
    """A delivered ("ready") agent's container must survive a checkpoint failure.

    A non-running existing record (e.g. "ready", set by phases/deliver.py)
    skips run_setup's fast path, but docker.provision() still resolves via
    docker-level reuse (reused=True) — register() succeeding on top of that
    doesn't mean THIS attempt's own docker.provision call created a fresh
    container. If on_registered then fails, the container must be preserved
    even though registered_by_this_call is True: only a genuinely fresh
    (non-reused) container is this attempt's own to tear down. The prior
    "ready" record this attempt overwrote must be restored verbatim, not
    merely deleted — deleting it would erase a delivered agent's record from
    the registry with nothing left to re-register it.
    """
    ready = _stored_env("a21", container_id="c-existing")  # status="ready" by default
    env_store = _rollback_env_store(get=ready, register_error=lambda *a, **kw: None)
    docker = _docker_stub(container_id="c-existing", container_name="agent-a21", reused=True)

    def on_registered(env_info):
        raise RuntimeError("checkpoint boom")

    _run_setup_expecting("checkpoint boom", "a21", env_store, docker, on_registered=on_registered)

    assert env_store.register.call_args_list[-1] == call(ready, fencing_token=None)
    env_store.remove.assert_not_called()
    docker.deprovision.assert_not_called()


def test_run_setup_calls_on_registered_with_fresh_environment(tmp_path: Path) -> None:
    """on_registered fires with the freshly registered EnvironmentInfo on success."""
    from agent_team_studio.agent_provisioning_team.phases.setup import run_setup
    from agent_team_studio.agent_provisioning_team.shared.environment_store import EnvironmentStore
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import ToolManifest

    env_store = EnvironmentStore(storage_dir=tmp_path)
    docker = _docker_stub(container_id="c-new", container_name="agent-a16")

    received = []
    result = run_setup(
        agent_id="a16",
        manifest=ToolManifest(),
        environment_store=env_store,
        docker_provisioner=docker,
        on_registered=received.append,
    )

    assert result.success is True
    assert len(received) == 1
    assert received[0].container_id == "c-new"
    assert result.environment.reused is False


def test_run_setup_skips_on_registered_on_fast_path(tmp_path: Path) -> None:
    """on_registered is not called when an already-running environment is reused.

    Nothing new is created on the fast path, so there is nothing for a
    checkpoint failure there to leak — the hook is scoped to fresh
    registrations only.
    """
    from agent_team_studio.agent_provisioning_team.phases.setup import run_setup
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import ToolManifest

    ready_env = _stored_env("a17", status="running")
    env_store = _rollback_env_store(get=ready_env)
    docker = _docker_stub()

    received = []
    result = run_setup(
        agent_id="a17",
        manifest=ToolManifest(),
        environment_store=env_store,
        docker_provisioner=docker,
        on_registered=received.append,
    )

    assert result.success is True
    assert received == []
    assert result.environment.reused is True


def test_run_setup_falls_through_fast_path_when_container_confirmed_gone(tmp_path: Path) -> None:
    """A "running" record whose container is CONFIRMED gone must not short-circuit.

    Trusting it would deliver success=True pointing at a dead container_id.
    Falling through to docker.provision() lets DockerProvisionerTool's own
    reuse check (_on_reuse) detect and clear the same staleness in its own
    idempotency state and create a fresh container instead.
    """
    from agent_team_studio.agent_provisioning_team.phases.setup import run_setup
    from agent_team_studio.agent_provisioning_team.shared.environment_store import EnvironmentStore
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import ToolManifest

    env_store = EnvironmentStore(storage_dir=tmp_path)
    env_store.register(_stored_env("a18", status="running", container_id="c-dead"))

    docker = _docker_stub(container_id="c-new", container_name="agent-a18")
    docker._container_exists = MagicMock(return_value=False)

    result = run_setup(
        agent_id="a18",
        manifest=ToolManifest(),
        environment_store=env_store,
        docker_provisioner=docker,
    )

    assert result.success is True
    docker.provision.assert_called_once()
    assert result.environment.container_id == "c-new"
    assert result.environment.reused is False


def test_run_setup_takes_fast_path_when_container_liveness_unknown(tmp_path: Path) -> None:
    """An inconclusive liveness probe (daemon unreachable) still trusts the record.

    Conservative default: unknown is not proof the container is gone, so
    falling through and potentially creating a duplicate container would be
    the wrong tradeoff.
    """
    from agent_team_studio.agent_provisioning_team.phases.setup import run_setup
    from agent_team_studio.agent_provisioning_team.shared.environment_store import EnvironmentStore
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import ToolManifest

    env_store = EnvironmentStore(storage_dir=tmp_path)
    env_store.register(_stored_env("a19", status="running", container_id="c-existing"))

    docker = _docker_stub()
    docker._container_exists = MagicMock(return_value=None)

    result = run_setup(
        agent_id="a19",
        manifest=ToolManifest(),
        environment_store=env_store,
        docker_provisioner=docker,
    )

    assert result.success is True
    docker.provision.assert_not_called()
    assert result.environment.container_id == "c-existing"


def test_run_setup_rollback_swallows_deprovision_error() -> None:
    """The best-effort rollback must not mask the original register failure."""
    env_store = _rollback_env_store(get=None)
    docker = _docker_stub(container_id="c-new", container_name="agent-a5")
    docker.deprovision.side_effect = RuntimeError("teardown boom")

    _run_setup_expecting("register boom", "a5", env_store, docker)

    docker.deprovision.assert_called_once_with("a5", fencing_token=None)


def test_run_setup_rollback_reports_failed_teardown(caplog) -> None:
    """A rollback whose teardown *reports* failure (not raises) must be logged."""
    from agent_team_studio.agent_provisioning_team.models import DeprovisionResult

    env_store = _rollback_env_store(get=None)
    docker = _docker_stub(container_id="c-new", container_name="agent-a6")
    docker.deprovision.return_value = DeprovisionResult(
        tool_name="docker", success=False, error="docker stop timed out"
    )

    with caplog.at_level(logging.ERROR):
        _run_setup_expecting("register boom", "a6", env_store, docker)

    docker.deprovision.assert_called_once_with("a6", fencing_token=None)
    assert "orphaned" in caplog.text
    assert "a6" in caplog.text


# ---------------------------------------------------------------------------
# credential_generation phase
# ---------------------------------------------------------------------------


def test_regenerate_credentials_creates_new_object(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.phases.credential_generation import (
        regenerate_credentials,
    )
    from agent_team_studio.agent_provisioning_team.shared.credential_store import CredentialStore

    cred_store = CredentialStore(storage_dir=tmp_path)
    result = regenerate_credentials("agent-x", "postgresql", credential_store=cred_store)
    assert result is not None
    assert result.tool_name == "postgresql"
    assert result.password


def test_regenerate_credentials_for_git_includes_token(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.phases.credential_generation import (
        regenerate_credentials,
    )
    from agent_team_studio.agent_provisioning_team.shared.credential_store import CredentialStore

    cred_store = CredentialStore(storage_dir=tmp_path)
    result = regenerate_credentials("agent-x", "git", credential_store=cred_store)
    assert result.token is not None


def test_get_stored_credentials_returns_empty_when_none(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.phases.credential_generation import (
        get_stored_credentials,
    )
    from agent_team_studio.agent_provisioning_team.shared.credential_store import CredentialStore

    cred_store = CredentialStore(storage_dir=tmp_path)
    out = get_stored_credentials("nobody", credential_store=cred_store)
    assert out == {}


def test_get_stored_credentials_roundtrip(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.phases.credential_generation import (
        get_stored_credentials,
        run_credential_generation,
    )
    from agent_team_studio.agent_provisioning_team.shared.credential_store import CredentialStore
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import (
        ToolDefinition,
        ToolManifest,
    )

    cred_store = CredentialStore(storage_dir=tmp_path)
    manifest = ToolManifest(
        tools=[
            ToolDefinition(
                name="postgresql",
                provisioner="postgres_provisioner",
                config={},
            )
        ]
    )

    result = run_credential_generation(
        agent_id="agent-y",
        manifest=manifest,
        credential_store=cred_store,
    )
    assert result.success is True
    assert "postgresql" in result.credentials

    fetched = get_stored_credentials("agent-y", credential_store=cred_store)
    assert "postgresql" in fetched


def test_get_stored_credentials_restores_enriched_fields(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.phases.credential_generation import (
        get_stored_credentials,
        store_credentials_payload,
    )
    from agent_team_studio.agent_provisioning_team.shared.credential_store import CredentialStore

    cred_store = CredentialStore(storage_dir=tmp_path)
    store_credentials_payload(
        "agent-z",
        "postgresql",
        {
            "username": "u",
            "password": "p",
            "connection_string": "postgres://u:p@host/db",
            "ssh_private_key": "PRIV",
            "extra": {"role": "app"},
        },
        credential_store=cred_store,
    )
    fetched = get_stored_credentials("agent-z", credential_store=cred_store)
    assert fetched["postgresql"].connection_string == "postgres://u:p@host/db"
    assert fetched["postgresql"].ssh_private_key == "PRIV"
    assert fetched["postgresql"].extra == {"role": "app"}


def test_run_credential_generation_with_progress_callback(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.phases.credential_generation import (
        run_credential_generation,
    )
    from agent_team_studio.agent_provisioning_team.shared.credential_store import CredentialStore
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import (
        ToolDefinition,
        ToolManifest,
    )

    progress_calls = []

    def cb(name, done, total):
        progress_calls.append((name, done, total))

    cred_store = CredentialStore(storage_dir=tmp_path)
    manifest = ToolManifest(
        tools=[
            ToolDefinition(name="pg", provisioner="postgres_provisioner", config={}),
        ]
    )

    result = run_credential_generation(
        agent_id="a",
        manifest=manifest,
        credential_store=cred_store,
        progress_callback=cb,
    )

    assert result.success is True
    # One per-tool call plus the final "complete" sentinel.
    assert any(c[0] == "complete" for c in progress_calls)


def test_run_credential_generation_rejects_stale_fencing_token(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.phases.credential_generation import (
        run_credential_generation,
    )
    from agent_team_studio.agent_provisioning_team.shared.credential_store import CredentialStore
    from agent_team_studio.agent_provisioning_team.shared.fencing import StaleFencingTokenError
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import (
        ToolDefinition,
        ToolManifest,
    )

    cred_store = CredentialStore(storage_dir=tmp_path)
    cred_store.store_credentials("a", "pg", {"password": "p1"}, fencing_token=5)
    manifest = ToolManifest(
        tools=[ToolDefinition(name="pg", provisioner="postgres_provisioner", config={})]
    )

    with pytest.raises(StaleFencingTokenError):
        run_credential_generation(
            agent_id="a", manifest=manifest, credential_store=cred_store, fencing_token=4
        )


def test_store_credentials_payload_threads_fencing_token(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.phases.credential_generation import (
        store_credentials_payload,
    )
    from agent_team_studio.agent_provisioning_team.shared.credential_store import CredentialStore
    from agent_team_studio.agent_provisioning_team.shared.fencing import StaleFencingTokenError

    cred_store = CredentialStore(storage_dir=tmp_path)
    store_credentials_payload(
        "a", "pg", {"username": "u", "password": "p"}, credential_store=cred_store, fencing_token=5
    )

    with pytest.raises(StaleFencingTokenError):
        store_credentials_payload(
            "a",
            "pg",
            {"username": "u", "password": "p2"},
            credential_store=cred_store,
            fencing_token=4,
        )


# ---------------------------------------------------------------------------
# account_provisioning phase
# ---------------------------------------------------------------------------


def test_run_account_provisioning_no_provisioner_registered() -> None:
    from agent_team_studio.agent_provisioning_team.phases.account_provisioning import (
        run_account_provisioning,
    )
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import (
        ToolDefinition,
        ToolManifest,
    )

    manifest = ToolManifest(
        tools=[
            ToolDefinition(name="weird", provisioner="generic_provisioner", config={}),
        ]
    )

    # Use a registry that maps to a different key so the lookup misses.
    fake_other = MagicMock()
    result = run_account_provisioning(
        agent_id="a1",
        manifest=manifest,
        credentials={"weird": GeneratedCredentials(tool_name="weird")},
        provisioners={"some_other_provisioner": fake_other},
    )
    assert result.success is False
    assert "Unknown provisioner" in result.tool_results[0].error


def test_run_account_provisioning_no_credentials() -> None:
    from agent_team_studio.agent_provisioning_team.phases.account_provisioning import (
        run_account_provisioning,
    )
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import (
        ToolDefinition,
        ToolManifest,
    )

    fake_prov = MagicMock()
    manifest = ToolManifest(
        tools=[
            ToolDefinition(name="t", provisioner="generic_provisioner", config={}),
        ]
    )

    result = run_account_provisioning(
        agent_id="a1",
        manifest=manifest,
        credentials={},  # no creds for "t"
        provisioners={"generic_provisioner": fake_prov},
    )
    assert result.success is False
    assert "No credentials" in result.tool_results[0].error


def test_run_account_provisioning_handles_provisioner_exception(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.phases.account_provisioning import (
        run_account_provisioning,
    )
    from agent_team_studio.agent_provisioning_team.shared.environment_store import EnvironmentStore
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import (
        ToolDefinition,
        ToolManifest,
    )

    bad_prov = MagicMock()
    bad_prov.provision.side_effect = RuntimeError("boom in provision")

    manifest = ToolManifest(
        tools=[ToolDefinition(name="t", provisioner="generic_provisioner", config={})]
    )

    result = run_account_provisioning(
        agent_id="a1",
        manifest=manifest,
        credentials={"t": GeneratedCredentials(tool_name="t")},
        provisioners={"generic_provisioner": bad_prov},
        environment_store=EnvironmentStore(storage_dir=tmp_path / "envs"),
    )

    assert result.success is False
    assert "boom" in result.tool_results[0].error


def test_run_account_provisioning_success(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.phases.account_provisioning import (
        run_account_provisioning,
    )
    from agent_team_studio.agent_provisioning_team.shared.environment_store import (
        EnvironmentInfo as StoreEnvInfo,
    )
    from agent_team_studio.agent_provisioning_team.shared.environment_store import EnvironmentStore
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import (
        ToolDefinition,
        ToolManifest,
    )

    env_store = EnvironmentStore(storage_dir=tmp_path / "envs")
    env_store.register(
        StoreEnvInfo(
            agent_id="a1",
            container_id="c1",
            container_name="agent-a1",
            workspace_path="/w",
        )
    )

    good = MagicMock()
    good.provision.return_value = ToolProvisionResult(
        tool_name="t",
        success=True,
        provisioner_key="generic_provisioner",
        permissions=["read"],
    )

    manifest = ToolManifest(
        tools=[ToolDefinition(name="t", provisioner="generic_provisioner", config={})]
    )

    cb_calls = []

    result = run_account_provisioning(
        agent_id="a1",
        manifest=manifest,
        credentials={"t": GeneratedCredentials(tool_name="t")},
        provisioners={"generic_provisioner": good},
        environment_store=env_store,
        progress_callback=lambda done, total, name: cb_calls.append((done, total, name)),
    )

    assert result.success is True
    assert result.tools_completed == 1
    assert any(c[2] == "complete" for c in cb_calls)


def test_run_account_provisioning_threads_fencing_token(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.phases.account_provisioning import (
        run_account_provisioning,
    )
    from agent_team_studio.agent_provisioning_team.shared.environment_store import (
        EnvironmentInfo as StoreEnvInfo,
    )
    from agent_team_studio.agent_provisioning_team.shared.environment_store import EnvironmentStore
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import (
        ToolDefinition,
        ToolManifest,
    )

    env_store = EnvironmentStore(storage_dir=tmp_path / "envs")
    env_store.register(
        StoreEnvInfo(
            agent_id="a1", container_id="c1", container_name="agent-a1", workspace_path="/w"
        ),
        fencing_token=5,
    )

    good = MagicMock()
    good.provision.return_value = ToolProvisionResult(
        tool_name="t", success=True, provisioner_key="generic_provisioner", permissions=["read"]
    )

    manifest = ToolManifest(
        tools=[ToolDefinition(name="t", provisioner="generic_provisioner", config={})]
    )

    result = run_account_provisioning(
        agent_id="a1",
        manifest=manifest,
        credentials={"t": GeneratedCredentials(tool_name="t")},
        provisioners={"generic_provisioner": good},
        environment_store=env_store,
        fencing_token=5,
    )

    assert result.success is True
    assert good.provision.call_args.kwargs["fencing_token"] == 5
    stored = env_store._read_env_data("a1")[0]
    assert stored["fencing_token"] == 5


def test_run_account_provisioning_propagates_stale_fencing_token_from_provision(
    tmp_path: Path,
) -> None:
    """A stale-token rejection is a caller/ownership error, not an ordinary
    tool failure -- it must propagate, not become a success=False result."""
    from agent_team_studio.agent_provisioning_team.phases.account_provisioning import (
        run_account_provisioning,
    )
    from agent_team_studio.agent_provisioning_team.shared.environment_store import EnvironmentStore
    from agent_team_studio.agent_provisioning_team.shared.fencing import StaleFencingTokenError
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import (
        ToolDefinition,
        ToolManifest,
    )

    bad_prov = MagicMock()
    bad_prov.provision.side_effect = StaleFencingTokenError("a1", "generic_provisioner", 4, 5)

    manifest = ToolManifest(
        tools=[ToolDefinition(name="t", provisioner="generic_provisioner", config={})]
    )

    with pytest.raises(StaleFencingTokenError):
        run_account_provisioning(
            agent_id="a1",
            manifest=manifest,
            credentials={"t": GeneratedCredentials(tool_name="t")},
            provisioners={"generic_provisioner": bad_prov},
            environment_store=EnvironmentStore(storage_dir=tmp_path / "envs"),
            fencing_token=4,
        )


def test_run_account_provisioning_propagates_stale_fencing_token_from_env_store(
    tmp_path: Path,
) -> None:
    from agent_team_studio.agent_provisioning_team.phases.account_provisioning import (
        run_account_provisioning,
    )
    from agent_team_studio.agent_provisioning_team.shared.fencing import StaleFencingTokenError
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import (
        ToolDefinition,
        ToolManifest,
    )

    good = MagicMock()
    good.provision.return_value = ToolProvisionResult(
        tool_name="t", success=True, provisioner_key="generic_provisioner", permissions=["read"]
    )
    env_store = MagicMock()
    env_store.add_tool.side_effect = StaleFencingTokenError("a1", "environment_store", 4, 5)

    manifest = ToolManifest(
        tools=[ToolDefinition(name="t", provisioner="generic_provisioner", config={})]
    )

    with pytest.raises(StaleFencingTokenError):
        run_account_provisioning(
            agent_id="a1",
            manifest=manifest,
            credentials={"t": GeneratedCredentials(tool_name="t")},
            provisioners={"generic_provisioner": good},
            environment_store=env_store,
            fencing_token=4,
        )


def test_run_account_provisioning_stamps_manifest_tool_name(tmp_path: Path) -> None:
    """The manifest's tool alias overrides the provisioner's own tool_name.

    A provisioner returns its own class-level tool_name (e.g. "postgresql"),
    which can differ from the manifest's alias for it (e.g. "pg") — but
    credentials are generated/stored under the MANIFEST name. Compensation's
    credential purge looks the entry up by result.tool_name, so leaving the
    provisioner's own name here would make it silently miss the credential
    actually stored for this tool. Mirrors provision_tool_activity (the
    Temporal path), which already does this override.
    """
    from agent_team_studio.agent_provisioning_team.phases.account_provisioning import (
        run_account_provisioning,
    )
    from agent_team_studio.agent_provisioning_team.shared.environment_store import EnvironmentStore
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import (
        ToolDefinition,
        ToolManifest,
    )

    aliased = MagicMock()
    aliased.provision.return_value = ToolProvisionResult(
        tool_name="postgresql",
        success=True,
        provisioner_key="postgres_provisioner",
        permissions=["read"],
    )

    manifest = ToolManifest(
        tools=[ToolDefinition(name="pg", provisioner="postgres_provisioner", config={})]
    )

    result = run_account_provisioning(
        agent_id="a1",
        manifest=manifest,
        credentials={"pg": GeneratedCredentials(tool_name="pg")},
        provisioners={"postgres_provisioner": aliased},
        environment_store=EnvironmentStore(storage_dir=tmp_path / "envs"),
    )

    assert result.tool_results[0].tool_name == "pg"


def test_deprovision_tools_all() -> None:
    from agent_team_studio.agent_provisioning_team.models import DeprovisionResult
    from agent_team_studio.agent_provisioning_team.phases.account_provisioning import (
        deprovision_tools,
    )

    p1 = MagicMock()
    p1.deprovision.return_value = DeprovisionResult(tool_name="p1", success=True)
    p2 = MagicMock()
    p2.deprovision.side_effect = RuntimeError("boom")

    results = deprovision_tools("a1", provisioners={"p1": p1, "p2": p2})
    assert results == {"p1": True, "p2": False}


def test_deprovision_tools_filtered() -> None:
    from agent_team_studio.agent_provisioning_team.models import DeprovisionResult
    from agent_team_studio.agent_provisioning_team.phases.account_provisioning import (
        deprovision_tools,
    )

    p1 = MagicMock()
    p1.deprovision.return_value = DeprovisionResult(tool_name="p1", success=True)
    p2 = MagicMock()
    p2.deprovision.return_value = DeprovisionResult(tool_name="p2", success=True)

    results = deprovision_tools("a1", provisioner_keys=["p1"], provisioners={"p1": p1, "p2": p2})
    assert results == {"p1": True}


def test_deprovision_tools_keys_by_provisioner_registry_key() -> None:
    # The result dict is keyed by the provisioner registry key from the
    # ``provisioners`` mapping, never by the ``tool_name`` a provisioner returns
    # (tools are many-to-one onto provisioners, so the two identities differ).
    from agent_team_studio.agent_provisioning_team.models import DeprovisionResult
    from agent_team_studio.agent_provisioning_team.phases.account_provisioning import (
        deprovision_tools,
    )

    prov = MagicMock()
    prov.deprovision.return_value = DeprovisionResult(tool_name="some_tool", success=True)

    results = deprovision_tools("a1", provisioners={"generic_provisioner": prov})
    assert results == {"generic_provisioner": True}


def test_deprovision_tools_threads_fencing_token() -> None:
    from agent_team_studio.agent_provisioning_team.models import DeprovisionResult
    from agent_team_studio.agent_provisioning_team.phases.account_provisioning import (
        deprovision_tools,
    )

    prov = MagicMock()
    prov.deprovision.return_value = DeprovisionResult(tool_name="p1", success=True)

    deprovision_tools("a1", provisioners={"p1": prov}, fencing_token=7)
    prov.deprovision.assert_called_once_with("a1", fencing_token=7)


def test_deprovision_tools_catches_stale_fencing_token_per_provisioner() -> None:
    """One provisioner's stale-token rejection must not abort the loop --
    each provisioner tracks its own high-water mark independently, so
    another, untouched provisioner may still legitimately accept the same
    token."""
    from agent_team_studio.agent_provisioning_team.models import DeprovisionResult
    from agent_team_studio.agent_provisioning_team.phases.account_provisioning import (
        deprovision_tools,
    )
    from agent_team_studio.agent_provisioning_team.shared.fencing import StaleFencingTokenError

    stale = MagicMock()
    stale.deprovision.side_effect = StaleFencingTokenError("a1", "p1", 4, 5)
    ok = MagicMock()
    ok.deprovision.return_value = DeprovisionResult(tool_name="p2", success=True)

    results = deprovision_tools("a1", provisioners={"p1": stale, "p2": ok}, fencing_token=4)
    assert results == {"p1": False, "p2": True}


def test_deprovision_tools_requires_agent_id() -> None:
    from agent_team_studio.agent_provisioning_team.phases.account_provisioning import (
        deprovision_tools,
    )

    with pytest.raises(AssertionError):
        deprovision_tools("", provisioners={})


def test_deprovision_tools_stops_at_cancellation_checkpoint() -> None:
    from agent_team_studio.agent_provisioning_team.models import (
        DeprovisionCancelledError,
        DeprovisionResult,
    )
    from agent_team_studio.agent_provisioning_team.phases.account_provisioning import (
        deprovision_tools,
    )

    p1 = MagicMock()
    p1.deprovision.return_value = DeprovisionResult(tool_name="p1", success=True)
    p2 = MagicMock()
    p2.deprovision.return_value = DeprovisionResult(tool_name="p2", success=True)
    p3 = MagicMock()
    p3.deprovision.return_value = DeprovisionResult(tool_name="p3", success=True)

    calls = {"n": 0}

    def checkpoint() -> bool:
        calls["n"] += 1
        return calls["n"] > 1  # let p1 run, cancel before p2's teardown call

    with pytest.raises(DeprovisionCancelledError) as exc_info:
        deprovision_tools("a1", provisioners={"p1": p1, "p2": p2, "p3": p3}, checkpoint=checkpoint)

    p1.deprovision.assert_called_once_with("a1", fencing_token=None)
    p2.deprovision.assert_not_called()
    p3.deprovision.assert_not_called()
    assert exc_info.value.agent_id == "a1"
    assert exc_info.value.completed == {"p1": True}


def test_build_default_tool_agents_for_account_provisioning() -> None:
    from agent_team_studio.agent_provisioning_team.shared.tool_agent_registry import (
        build_default_tool_agents,
    )

    out = build_default_tool_agents()
    assert isinstance(out, dict)
    assert "docker_provisioner" in out


# ---------------------------------------------------------------------------
# access_audit phase
# ---------------------------------------------------------------------------


def test_run_access_audit_all_success() -> None:
    from agent_team_studio.agent_provisioning_team.phases.access_audit import run_access_audit

    tool_results = [
        ToolProvisionResult(
            tool_name="t1",
            success=True,
            permissions=["read", "write"],
            provisioner_key="x",
        )
    ]
    msgs = []
    result = run_access_audit(
        agent_id="a1",
        tool_results=tool_results,
        progress_callback=lambda m: msgs.append(m),
    )
    assert result.passed is True
    assert len(result.verifications) == 1
    assert "read" in result.verifications[0].actual_permissions
    assert any("Starting" in m for m in msgs)


def test_run_access_audit_with_failed_tool() -> None:
    from agent_team_studio.agent_provisioning_team.phases.access_audit import run_access_audit

    tool_results = [
        ToolProvisionResult(tool_name="t1", success=False, error="rpc failed", provisioner_key="x"),
        ToolProvisionResult(
            tool_name="t2", success=True, permissions=["read"], provisioner_key="x"
        ),
    ]
    result = run_access_audit(agent_id="a1", tool_results=tool_results)
    assert result.passed is False
    assert any(not v.passed for v in result.verifications)
    assert any("rpc failed" in v.errors[0] for v in result.verifications if not v.passed)


def test_audit_single_tool_returns_provisioner_result() -> None:
    from agent_team_studio.agent_provisioning_team.models import AccessVerification
    from agent_team_studio.agent_provisioning_team.phases.access_audit import audit_single_tool

    prov = MagicMock()
    prov.verify_access.return_value = AccessVerification(
        tool_name="t", passed=True, actual_permissions=["read"]
    )

    v = audit_single_tool("a1", "t", provisioner=prov)
    assert v.passed is True


def test_audit_single_tool_no_provisioner_returns_error() -> None:
    from agent_team_studio.agent_provisioning_team.phases.access_audit import audit_single_tool

    with patch(
        "agent_team_studio.agent_provisioning_team.phases.access_audit.build_default_tool_agents",
        return_value={},
    ):
        v = audit_single_tool("a1", "nonexistent")
    assert v.passed is False
    assert "No provisioner" in v.errors[0]


def test_audit_single_tool_uses_provisioner_key_when_name_mismatches_registry() -> None:
    """Regression test: tool_name and registry key stems can differ (e.g.
    "postgresql" tool name vs "postgres_provisioner" registry key), so
    audit_single_tool must resolve via the explicit provisioner_key rather
    than guessing f"{tool_name}_provisioner"."""
    from agent_team_studio.agent_provisioning_team.models import AccessVerification
    from agent_team_studio.agent_provisioning_team.phases.access_audit import audit_single_tool

    postgres_prov = MagicMock()
    postgres_prov.verify_access.return_value = AccessVerification(
        tool_name="postgresql", passed=True, actual_permissions=["read", "write"]
    )
    registry = {"postgres_provisioner": postgres_prov}

    with patch(
        "agent_team_studio.agent_provisioning_team.phases.access_audit.build_default_tool_agents",
        return_value=registry,
    ):
        v = audit_single_tool("a1", "postgresql", provisioner_key="postgres_provisioner")
    assert v.passed is True
    postgres_prov.verify_access.assert_called_once_with("a1")

    # Without the key, the fragile f"{tool_name}_provisioner" guess
    # ("postgresql_provisioner") still fails to find the same registry entry.
    with patch(
        "agent_team_studio.agent_provisioning_team.phases.access_audit.build_default_tool_agents",
        return_value=registry,
    ):
        v_no_key = audit_single_tool("a1", "postgresql")
    assert v_no_key.passed is False
    assert "No provisioner" in v_no_key.errors[0]


def test_generate_audit_report_includes_status() -> None:
    from agent_team_studio.agent_provisioning_team.models import AccessVerification
    from agent_team_studio.agent_provisioning_team.phases.access_audit import generate_audit_report

    audit = AccessAuditResult(
        passed=True,
        verifications=[
            AccessVerification(
                tool_name="t1",
                passed=True,
                actual_permissions=["read"],
                warnings=["wide perms"],
            ),
            AccessVerification(
                tool_name="t2",
                passed=False,
                actual_permissions=[],
                errors=["fail"],
            ),
        ],
        warnings=["overall warning"],
        errors=["overall error"],
    )

    report = generate_audit_report(audit)
    assert "PASSED" in report
    assert "t1" in report and "t2" in report
    assert "wide perms" in report
    assert "fail" in report
    assert "overall warning" in report
    assert "overall error" in report


def test_generate_audit_report_failed_status() -> None:
    from agent_team_studio.agent_provisioning_team.phases.access_audit import generate_audit_report

    audit = AccessAuditResult(passed=False, verifications=[])
    report = generate_audit_report(audit)
    assert "FAILED" in report


# ---------------------------------------------------------------------------
# documentation phase
# ---------------------------------------------------------------------------


def test_run_documentation_full_path(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.phases.documentation import run_documentation
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import (
        ToolDefinition,
        ToolManifest,
    )

    manifest = ToolManifest(
        tools=[
            ToolDefinition(
                name="postgresql",
                provisioner="postgres_provisioner",
                config={},
                onboarding={
                    "description": "PG db",
                    "env_var": "POSTGRES_URL",
                    "getting_started": "Connect to {username}",
                },
            ),
        ]
    )

    creds = GeneratedCredentials(
        tool_name="postgresql",
        username="agent_u",
        password="pw",
        connection_string="postgresql://agent_u:pw@h:5432/db",
    )

    tool_results = [
        ToolProvisionResult(
            tool_name="postgresql",
            success=True,
            permissions=["ALL PRIVILEGES"],
            provisioner_key="postgres_provisioner",
        )
    ]

    ws = tmp_path / "ws"
    ws.mkdir()
    msgs = []
    result = run_documentation(
        agent_id="agent-1",
        manifest=manifest,
        credentials={"postgresql": creds},
        tool_results=tool_results,
        workspace_path=str(ws),
        progress_callback=lambda m: msgs.append(m),
    )

    assert result.success is True
    assert result.onboarding is not None
    assert any(t.name == "postgresql" for t in result.onboarding.tools)
    assert result.onboarding.environment_variables["POSTGRES_URL"]
    assert result.onboarding.environment_variables["AGENT_ID"] == "agent-1"


def test_run_documentation_skips_failed_tools(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.phases.documentation import run_documentation
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import (
        ToolDefinition,
        ToolManifest,
    )

    # The failed tool IS declared in the manifest, so it can only be omitted
    # from the onboarding packet because its provisioning failed (the
    # ``r.success`` filter) — not because it is absent from the manifest.
    manifest = ToolManifest(
        tools=[
            ToolDefinition(
                name="failed",
                provisioner="postgres_provisioner",
                config={},
                onboarding={"description": "db"},
            ),
        ]
    )

    tool_results = [
        ToolProvisionResult(tool_name="failed", success=False, error="x", provisioner_key="y")
    ]

    result = run_documentation(
        agent_id="a1",
        manifest=manifest,
        credentials={},
        tool_results=tool_results,
        workspace_path=str(tmp_path),
    )

    assert result.success is True
    assert result.onboarding.tools == []


def test_run_documentation_skips_tool_not_in_manifest(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.phases.documentation import run_documentation
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import ToolManifest

    manifest = ToolManifest()  # No tools

    tool_results = [
        ToolProvisionResult(tool_name="ghost", success=True, permissions=[], provisioner_key="x")
    ]

    result = run_documentation(
        agent_id="a1",
        manifest=manifest,
        credentials={},
        tool_results=tool_results,
        workspace_path=str(tmp_path),
    )
    assert result.success is True
    # ghost isn't in manifest → no tool docs
    assert result.onboarding.tools == []


def test_run_documentation_uses_default_getting_started_template(tmp_path: Path) -> None:
    """If a tool has no getting_started config, the deterministic fallback runs."""
    from agent_team_studio.agent_provisioning_team.phases.documentation import run_documentation
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import (
        ToolDefinition,
        ToolManifest,
    )

    manifest = ToolManifest(
        tools=[
            ToolDefinition(
                name="redis",
                provisioner="redis_provisioner",
                config={},
                onboarding={
                    "description": "Redis",
                    "env_var": "REDIS_URL",
                    "getting_started": "",
                },
            ),
        ]
    )

    creds = GeneratedCredentials(
        tool_name="redis",
        username="u",
        password="p",
        connection_string="redis://u:p@h:6379",
    )

    tool_results = [
        ToolProvisionResult(
            tool_name="redis",
            success=True,
            permissions=["+@all"],
            provisioner_key="redis_provisioner",
        )
    ]

    result = run_documentation(
        agent_id="a1",
        manifest=manifest,
        credentials={"redis": creds},
        tool_results=tool_results,
        workspace_path=str(tmp_path),
    )

    tool_doc = result.onboarding.tools[0]
    # Template fallback mentions the env var
    assert "REDIS_URL" in tool_doc.getting_started


def test_generate_readme_includes_sections() -> None:
    from agent_team_studio.agent_provisioning_team.phases.documentation import generate_readme

    onboarding = OnboardingPacket(
        summary="hello",
        tools=[
            ToolOnboardingInfo(
                name="pg",
                description="Postgres",
                env_var="POSTGRES_URL",
                getting_started="psql $POSTGRES_URL",
                permissions=["ALL PRIVILEGES"],
            )
        ],
        environment_variables={
            "POSTGRES_URL": "postgresql://u:pw@h/db",
            "AGENT_ID": "a1",
        },
        anatomy_bundle_path="/ws/docs/agent_anatomy",
    )

    out = generate_readme(onboarding)
    assert "# Agent Workspace" in out
    assert "## Available Tools" in out
    assert "POSTGRES_URL" in out
    assert "ALL PRIVILEGES" in out
    assert "agent_anatomy" in out
    # POSTGRES_URL key does not contain "password", so no redaction placeholder appears
    assert "***" not in out


def test_generate_readme_redacts_password_envvar() -> None:
    from agent_team_studio.agent_provisioning_team.phases.documentation import generate_readme

    onboarding = OnboardingPacket(
        summary="hi",
        tools=[],
        environment_variables={"DB_PASSWORD": "secret123"},
    )
    out = generate_readme(onboarding)
    assert "secret123" not in out
    assert "***" in out


def test_generate_readme_no_anatomy_bundle() -> None:
    from agent_team_studio.agent_provisioning_team.phases.documentation import generate_readme

    onboarding = OnboardingPacket(summary="s", tools=[], environment_variables={})
    out = generate_readme(onboarding)
    # Falls through to the "when the workspace path is available" message.
    assert "docs/agent_anatomy" in out


# ---------------------------------------------------------------------------
# deliver phase
# ---------------------------------------------------------------------------


def test_run_deliver_updates_status(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.phases.deliver import run_deliver
    from agent_team_studio.agent_provisioning_team.shared.environment_store import (
        EnvironmentInfo as StoreEnvInfo,
    )
    from agent_team_studio.agent_provisioning_team.shared.environment_store import EnvironmentStore

    env_store = EnvironmentStore(storage_dir=tmp_path)
    env_store.register(
        StoreEnvInfo(
            agent_id="a1",
            container_id="c1",
            container_name="agent-a1",
            workspace_path="/w",
        )
    )

    env = EnvironmentInfo(container_id="c1", container_name="agent-a1")
    msgs = []
    result = run_deliver(
        agent_id="a1",
        environment=env,
        credentials={},
        tool_results=[],
        access_audit=None,
        onboarding=None,
        environment_store=env_store,
        progress_callback=lambda m: msgs.append(m),
    )

    assert result.success is True
    # status was bumped to "ready"
    env_after = env_store.get("a1")
    assert env_after.status == "ready"


def test_run_deliver_rejects_stale_fencing_token(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.phases.deliver import run_deliver
    from agent_team_studio.agent_provisioning_team.shared.environment_store import (
        EnvironmentInfo as StoreEnvInfo,
    )
    from agent_team_studio.agent_provisioning_team.shared.environment_store import EnvironmentStore
    from agent_team_studio.agent_provisioning_team.shared.fencing import StaleFencingTokenError

    env_store = EnvironmentStore(storage_dir=tmp_path)
    env_store.register(
        StoreEnvInfo(
            agent_id="a1", container_id="c1", container_name="agent-a1", workspace_path="/w"
        ),
        fencing_token=5,
    )

    env = EnvironmentInfo(container_id="c1", container_name="agent-a1")
    with pytest.raises(StaleFencingTokenError):
        run_deliver(
            agent_id="a1",
            environment=env,
            credentials={},
            tool_results=[],
            access_audit=None,
            onboarding=None,
            environment_store=env_store,
            fencing_token=4,
        )

    # The rejected call did not bump status.
    assert env_store.get("a1").status == "running"


def test_run_deliver_without_environment_does_not_update(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.phases.deliver import run_deliver
    from agent_team_studio.agent_provisioning_team.shared.environment_store import EnvironmentStore

    env_store = EnvironmentStore(storage_dir=tmp_path)
    result = run_deliver(
        agent_id="a1",
        environment=None,
        credentials={},
        tool_results=[],
        access_audit=None,
        onboarding=None,
        environment_store=env_store,
    )
    assert result.success is True


def test_build_final_result_success() -> None:
    from agent_team_studio.agent_provisioning_team.phases.deliver import build_final_result

    env = EnvironmentInfo(container_id="c1", container_name="c1")
    tr = [ToolProvisionResult(tool_name="t", success=True, provisioner_key="x")]
    final = build_final_result(
        agent_id="a1",
        environment=env,
        credentials={},
        tool_results=tr,
        access_audit=AccessAuditResult(passed=True, verifications=[]),
        onboarding=None,
        deliver_result=DeliverResult(success=True),
    )
    assert final.success is True
    assert final.current_phase == Phase.DELIVER


def test_build_final_result_with_failures() -> None:
    from agent_team_studio.agent_provisioning_team.phases.deliver import build_final_result

    tr = [
        ToolProvisionResult(tool_name="t1", success=False, error="boom", provisioner_key="x"),
    ]
    final = build_final_result(
        agent_id="a1",
        environment=None,  # missing env
        credentials={},
        tool_results=tr,
        access_audit=AccessAuditResult(passed=False, verifications=[]),
        onboarding=None,
        deliver_result=DeliverResult(success=True),
    )
    assert final.success is False
    assert "Environment setup failed" in final.error
    assert "t1" in final.error
    assert "Access audit failed" in final.error


def test_redact_connection_string_no_password() -> None:
    from agent_team_studio.agent_provisioning_team.phases.deliver import _redact_connection_string

    assert _redact_connection_string(None) is None
    assert _redact_connection_string("") is None
    # Already-redacted strings are left untouched (no `:pw@` pattern).
    assert _redact_connection_string("plain") == "plain"


def test_redact_credentials_with_ssh_key() -> None:
    from agent_team_studio.agent_provisioning_team.phases.deliver import (
        redact_credentials_for_response,
    )

    result = ProvisioningResult(
        agent_id="a1",
        success=True,
        credentials={
            "git": GeneratedCredentials(
                tool_name="git",
                ssh_private_key="PRIVATE",
                ssh_public_key="PUBLIC",
                extra={"workspace_path": "/w", "password": "secret"},
            ),
        },
    )
    redacted = redact_credentials_for_response(result)
    assert redacted.credentials["git"].ssh_private_key == "***"
    assert redacted.credentials["git"].ssh_public_key == "PUBLIC"
    assert "password" not in redacted.credentials["git"].extra


def test_redact_details_scalar_passthrough() -> None:
    from agent_team_studio.agent_provisioning_team.phases.deliver import _redact_details

    assert _redact_details(123) == 123
    assert _redact_details(True) is True
    assert _redact_details(None) is None


def test_redact_details_handles_list() -> None:
    from agent_team_studio.agent_provisioning_team.phases.deliver import _redact_details

    out = _redact_details([{"password": "x"}, {"safe": "y"}])
    assert out[0]["password"] == "***"
    assert out[1]["safe"] == "y"


# -------------------------------------------------------------------------
# documentation phase LLM summary / getting-started happy + fallback paths.
# -------------------------------------------------------------------------


def test_documentation_uses_llm_summary_when_configured(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.phases import documentation as doc_mod
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import ToolManifest

    captured = {}

    class _StubClient:
        def complete(self, prompt, **kwargs):
            captured["called"] = True
            captured["kwargs"] = kwargs
            return "FAKE_LLM_SUMMARY"

    def make_client(agent_key=None):
        captured["agent_key"] = agent_key
        return _StubClient()

    with patch.object(doc_mod, "get_client", make_client):
        result = doc_mod.run_documentation(
            agent_id="a1",
            manifest=ToolManifest(),
            credentials={},
            tool_results=[],
            workspace_path=str(tmp_path),
        )
    assert result.success is True
    assert "FAKE_LLM_SUMMARY" in result.onboarding.summary
    assert captured["called"] is True
    assert captured["agent_key"] == "agent_provisioning_team.documentation"
    assert captured["kwargs"].get("max_tokens") == 300


def test_documentation_llm_summary_falls_back_on_exception(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.phases import documentation as doc_mod
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import ToolManifest

    class _BoomClient:
        def complete(self, prompt, **kwargs):
            raise RuntimeError("api down")

    with patch.object(doc_mod, "get_client", lambda agent_key=None: _BoomClient()):
        result = doc_mod.run_documentation(
            agent_id="a1",
            manifest=ToolManifest(),
            credentials={},
            tool_results=[],
            workspace_path=str(tmp_path),
        )
    # Falls back to deterministic template
    assert "tool(s) configured" in result.onboarding.summary


def test_documentation_summary_uses_template_for_dummy_client(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.phases import documentation as doc_mod
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import ToolManifest
    from llm_service import DummyLLMClient

    with patch.object(doc_mod, "get_client", lambda agent_key=None: DummyLLMClient()):
        result = doc_mod.run_documentation(
            agent_id="a1",
            manifest=ToolManifest(),
            credentials={},
            tool_results=[],
            workspace_path=str(tmp_path),
        )
    # A DummyLLMClient is treated the same as "unconfigured": template, not dummy text.
    assert "tool(s) configured" in result.onboarding.summary


def test_documentation_uses_llm_getting_started_when_configured(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.models import (
        GeneratedCredentials,
        ToolProvisionResult,
    )
    from agent_team_studio.agent_provisioning_team.phases import documentation as doc_mod
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import (
        ToolDefinition,
        ToolManifest,
    )

    manifest = ToolManifest(
        tools=[
            ToolDefinition(
                name="redis",
                provisioner="redis_provisioner",
                config={},
                onboarding={
                    "description": "Redis",
                    "env_var": "REDIS_URL",
                    "getting_started": "",  # empty → triggers LLM path
                },
            ),
        ]
    )

    # run_documentation also calls _generate_summary against the same stubbed
    # client, so calls/agent_keys are collected per-call rather than
    # overwritten — the getting-started call is picked out by its objective.
    calls = []
    agent_keys = []

    class _StubClient:
        def complete(self, prompt, **kwargs):
            calls.append(kwargs)
            return "FAKE_TOOL_DOC"

    def make_client(agent_key=None):
        agent_keys.append(agent_key)
        return _StubClient()

    with patch.object(doc_mod, "get_client", make_client):
        result = doc_mod.run_documentation(
            agent_id="a1",
            manifest=manifest,
            credentials={
                "redis": GeneratedCredentials(tool_name="redis", connection_string="redis://x")
            },
            tool_results=[
                ToolProvisionResult(
                    tool_name="redis",
                    success=True,
                    permissions=["+@all"],
                    provisioner_key="redis_provisioner",
                )
            ],
            workspace_path=str(tmp_path),
        )

    # LLM-generated docs appear in the tool's getting_started field
    assert any("FAKE_TOOL_DOC" in t.getting_started for t in result.onboarding.tools)
    assert all(key == "agent_provisioning_team.documentation" for key in agent_keys)
    getting_started_call = next(
        c for c in calls if c.get("objective") == "generate tool getting-started guide"
    )
    assert getting_started_call.get("max_tokens") == 400


def test_documentation_llm_getting_started_falls_back_on_exception(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.models import (
        GeneratedCredentials,
        ToolProvisionResult,
    )
    from agent_team_studio.agent_provisioning_team.phases import documentation as doc_mod
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import (
        ToolDefinition,
        ToolManifest,
    )

    manifest = ToolManifest(
        tools=[
            ToolDefinition(
                name="redis",
                provisioner="redis_provisioner",
                config={},
                onboarding={
                    "description": "Redis",
                    "env_var": "REDIS_URL",
                    "getting_started": "",
                },
            ),
        ]
    )

    class _BoomClient:
        def complete(self, prompt, **kwargs):
            raise RuntimeError("api down")

    with patch.object(doc_mod, "get_client", lambda agent_key=None: _BoomClient()):
        result = doc_mod.run_documentation(
            agent_id="a1",
            manifest=manifest,
            credentials={
                "redis": GeneratedCredentials(tool_name="redis", connection_string="redis://x")
            },
            tool_results=[
                ToolProvisionResult(
                    tool_name="redis",
                    success=True,
                    permissions=["+@all"],
                    provisioner_key="redis_provisioner",
                )
            ],
            workspace_path=str(tmp_path),
        )
    # Falls back to deterministic template (mentions env var).
    assert any("REDIS_URL" in t.getting_started for t in result.onboarding.tools)


def test_documentation_getting_started_uses_template_for_dummy_client(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.models import (
        GeneratedCredentials,
        ToolProvisionResult,
    )
    from agent_team_studio.agent_provisioning_team.phases import documentation as doc_mod
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import (
        ToolDefinition,
        ToolManifest,
    )
    from llm_service import DummyLLMClient

    manifest = ToolManifest(
        tools=[
            ToolDefinition(
                name="redis",
                provisioner="redis_provisioner",
                config={},
                onboarding={
                    "description": "Redis",
                    "env_var": "REDIS_URL",
                    "getting_started": "",
                },
            ),
        ]
    )

    with patch.object(doc_mod, "get_client", lambda agent_key=None: DummyLLMClient()):
        result = doc_mod.run_documentation(
            agent_id="a1",
            manifest=manifest,
            credentials={
                "redis": GeneratedCredentials(tool_name="redis", connection_string="redis://x")
            },
            tool_results=[
                ToolProvisionResult(
                    tool_name="redis",
                    success=True,
                    permissions=["+@all"],
                    provisioner_key="redis_provisioner",
                )
            ],
            workspace_path=str(tmp_path),
        )
    # A DummyLLMClient is treated the same as "unconfigured": template, not dummy text.
    assert any("REDIS_URL" in t.getting_started for t in result.onboarding.tools)


def test_documentation_getting_started_template_substitutes_username(tmp_path: Path) -> None:
    """{username} and {connection_string} placeholders get substituted from creds.extra."""
    from agent_team_studio.agent_provisioning_team.models import (
        GeneratedCredentials,
        ToolProvisionResult,
    )
    from agent_team_studio.agent_provisioning_team.phases.documentation import run_documentation
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import (
        ToolDefinition,
        ToolManifest,
    )

    manifest = ToolManifest(
        tools=[
            ToolDefinition(
                name="pg",
                provisioner="postgres_provisioner",
                config={},
                onboarding={
                    "description": "PG",
                    "getting_started": "user={username} extra={port}",
                },
            ),
        ]
    )

    creds = GeneratedCredentials(
        tool_name="pg",
        username="u1",
        password="p",
        connection_string="conn",
        extra={"port": 5432},
    )
    tool_results = [
        ToolProvisionResult(
            tool_name="pg",
            success=True,
            permissions=["ALL"],
            provisioner_key="postgres_provisioner",
        )
    ]

    result = run_documentation(
        agent_id="a1",
        manifest=manifest,
        credentials={"pg": creds},
        tool_results=tool_results,
        workspace_path=str(tmp_path),
    )

    rendered = result.onboarding.tools[0].getting_started
    assert "user=u1" in rendered
    assert "extra=5432" in rendered


def test_documentation_getting_started_template_skips_unsafe_extra_key(tmp_path: Path) -> None:
    """A credentials.extra key containing braces is skipped, not used to build a malformed
    replacement target — the rest of the template still renders, including sibling keys."""
    from agent_team_studio.agent_provisioning_team.models import (
        GeneratedCredentials,
        ToolProvisionResult,
    )
    from agent_team_studio.agent_provisioning_team.phases.documentation import run_documentation
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import (
        ToolDefinition,
        ToolManifest,
    )

    manifest = ToolManifest(
        tools=[
            ToolDefinition(
                name="pg",
                provisioner="postgres_provisioner",
                config={},
                onboarding={
                    "description": "PG",
                    "getting_started": "user={username} extra={port} literal={port}extra{",
                },
            ),
        ]
    )

    creds = GeneratedCredentials(
        tool_name="pg",
        username="u1",
        password="p",
        connection_string="conn",
        extra={"port": 5432, "port}extra{": "evil"},
    )
    tool_results = [
        ToolProvisionResult(
            tool_name="pg",
            success=True,
            permissions=["ALL"],
            provisioner_key="postgres_provisioner",
        )
    ]

    result = run_documentation(
        agent_id="a1",
        manifest=manifest,
        credentials={"pg": creds},
        tool_results=tool_results,
        workspace_path=str(tmp_path),
    )

    rendered = result.onboarding.tools[0].getting_started
    # The well-formed "port" key still substitutes normally.
    assert "user=u1" in rendered
    assert "extra=5432" in rendered
    # The unsafe key "port}extra{" is never used to build a replacement target, so the
    # literal template text it would have malformed is left untouched and "evil" never appears.
    assert "literal={port}extra{" in rendered
    assert "evil" not in rendered


def test_documentation_getting_started_template_sanitizes_credentials(tmp_path: Path) -> None:
    """Credential values interpolated into a getting_started template are sanitized."""
    from agent_team_studio.agent_provisioning_team.models import (
        GeneratedCredentials,
        ToolProvisionResult,
    )
    from agent_team_studio.agent_provisioning_team.phases.documentation import run_documentation
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import (
        ToolDefinition,
        ToolManifest,
    )

    manifest = ToolManifest(
        tools=[
            ToolDefinition(
                name="pg",
                provisioner="postgres_provisioner",
                config={},
                onboarding={
                    "description": "PG",
                    "getting_started": "user={username} conn={connection_string} note={note}",
                },
            ),
        ]
    )

    creds = GeneratedCredentials(
        tool_name="pg",
        username="u1\x00",
        password="p",
        connection_string="conn\x00string",
        extra={"note": "hi\x00there"},
    )
    tool_results = [
        ToolProvisionResult(
            tool_name="pg",
            success=True,
            permissions=["ALL"],
            provisioner_key="postgres_provisioner",
        )
    ]

    result = run_documentation(
        agent_id="a1",
        manifest=manifest,
        credentials={"pg": creds},
        tool_results=tool_results,
        workspace_path=str(tmp_path),
    )

    rendered = result.onboarding.tools[0].getting_started
    assert "\x00" not in rendered
    assert "user=u1" in rendered
    assert "conn=connstring" in rendered
    assert "note=hithere" in rendered
