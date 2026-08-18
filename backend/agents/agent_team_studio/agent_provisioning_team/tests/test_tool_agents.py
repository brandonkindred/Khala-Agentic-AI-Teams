"""Unit tests for tool provisioner agents.

Docker / Postgres / Redis / Git are all mocked at the
subprocess / library boundary — no real services are touched.
"""

from __future__ import annotations

import logging
import subprocess as _subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent_team_studio.agent_provisioning_team.models import GeneratedCredentials
from agent_team_studio.agent_provisioning_team.shared.provisioner_state import (
    CompensationRecord,
    ProvisionerStateStore,
)
from agent_team_studio.agent_provisioning_team.tool_agents.base import BaseToolProvisioner

# ---------------------------------------------------------------------------
# base.py
# ---------------------------------------------------------------------------


class _MinimalProv(BaseToolProvisioner):
    tool_name = "minimal"

    def __init__(self, storage_dir: Path) -> None:
        self._state = ProvisionerStateStore("minimal_prov", storage_dir=storage_dir)

    def provision(self, agent_id, config, credentials, fencing_token=None):
        return self.run_idempotent(
            agent_id,
            credentials=credentials,
            create=lambda _r: (["read"], {"x": 1, "permissions": ["read"]}),
            fencing_token=fencing_token,
        )

    def verify_access(self, agent_id):
        return self._make_verification(passed=True, actual_permissions=[])

    def deprovision(self, agent_id):
        from agent_team_studio.agent_provisioning_team.models import DeprovisionResult

        return DeprovisionResult(tool_name=self.tool_name, success=True)


def test_canonical_anatomy_preamble_returns_text() -> None:
    text = BaseToolProvisioner.canonical_anatomy_prompt_preamble()
    assert isinstance(text, str)
    assert "anatomy" in text.lower() or "Khala" in text


def test_make_error_result_shape() -> None:
    prov = _MinimalProv(Path("/tmp/test-state-1"))
    out = prov._make_error_result("nope")
    assert out.success is False
    assert out.error == "nope"
    assert out.tool_name == "minimal"


def test_make_verification_with_warnings_errors() -> None:
    prov = _MinimalProv(Path("/tmp/test-state-2"))
    v = prov._make_verification(
        passed=False,
        actual_permissions=["read"],
        warnings=["w1"],
        errors=["e1"],
    )
    assert v.passed is False
    assert v.warnings == ["w1"]
    assert v.errors == ["e1"]


def test_replay_compensation_default_logs_warning(tmp_path: Path, caplog) -> None:
    prov = _MinimalProv(tmp_path)
    # Default replay_compensation just logs and returns None
    prov.replay_compensation("a", "unknown.kind", {"data": 1})
    # Function returns nothing; nothing raises.


def test_run_idempotent_handles_filenotfound(tmp_path: Path) -> None:
    class _FNFProv(BaseToolProvisioner):
        tool_name = "fnf"

        def __init__(self) -> None:
            self._state = ProvisionerStateStore("fnf_prov", storage_dir=tmp_path)

        def provision(self, agent_id, config, credentials):
            def _create(_r):
                raise FileNotFoundError("git not found")

            return self.run_idempotent(agent_id, credentials=credentials, create=_create)

        def verify_access(self, agent_id):
            return self._make_verification(passed=True, actual_permissions=[])

        def deprovision(self, agent_id):
            from agent_team_studio.agent_provisioning_team.models import DeprovisionResult

            return DeprovisionResult(tool_name=self.tool_name, success=True)

    out = _FNFProv().provision("a", {}, GeneratedCredentials(tool_name="x"))
    assert out.success is False
    assert "binary not found" in out.error


def test_run_idempotent_handles_permission_error(tmp_path: Path) -> None:
    class _PermProv(BaseToolProvisioner):
        tool_name = "perm"

        def __init__(self) -> None:
            self._state = ProvisionerStateStore("perm_prov", storage_dir=tmp_path)

        def provision(self, agent_id, config, credentials):
            def _create(_r):
                raise PermissionError("nope")

            return self.run_idempotent(agent_id, credentials=credentials, create=_create)

        def verify_access(self, agent_id):
            return self._make_verification(passed=True, actual_permissions=[])

        def deprovision(self, agent_id):
            from agent_team_studio.agent_provisioning_team.models import DeprovisionResult

            return DeprovisionResult(tool_name=self.tool_name, success=True)

    out = _PermProv().provision("a", {}, GeneratedCredentials(tool_name="x"))
    assert out.success is False
    assert "permission denied" in out.error


def test_list_and_clear_compensations(tmp_path: Path) -> None:
    prov = _MinimalProv(tmp_path)
    prov._state.add_compensation("a", CompensationRecord(kind="k1", payload={}))
    assert len(prov.list_compensations("a")) == 1
    prov.clear_compensations("a")
    assert prov.list_compensations("a") == []


def test_run_idempotent_hydrates_extras(tmp_path: Path) -> None:
    class _HydrateProv(BaseToolProvisioner):
        tool_name = "hyd"

        def __init__(self) -> None:
            self._state = ProvisionerStateStore("hyd_prov", storage_dir=tmp_path)

        def provision(self, agent_id, config, credentials):
            return self.run_idempotent(
                agent_id,
                credentials=credentials,
                create=lambda _r: (
                    ["all"],
                    {"workspace_path": "/ws", "permissions": ["all"]},
                ),
                hydrate_extras=("workspace_path",),
            )

        def verify_access(self, agent_id):
            return self._make_verification(passed=True, actual_permissions=[])

        def deprovision(self, agent_id):
            from agent_team_studio.agent_provisioning_team.models import DeprovisionResult

            return DeprovisionResult(tool_name=self.tool_name, success=True)

    prov = _HydrateProv()
    creds1 = GeneratedCredentials(tool_name="hyd")
    first = prov.provision("a", {}, creds1)
    assert first.success
    assert first.details["workspace_path"] == "/ws"

    # Second call hydrates extras into a fresh creds object
    creds2 = GeneratedCredentials(tool_name="hyd")
    second = prov.provision("a", {}, creds2)
    assert second.success
    assert second.details.get("reused") is True
    assert creds2.extra.get("workspace_path") == "/ws"


def test_run_idempotent_rejects_stale_token_before_create_runs(tmp_path: Path) -> None:
    """The fencing preflight must reject BEFORE create() runs, not after --
    create() is where the real infrastructure side effect happens."""
    from agent_team_studio.agent_provisioning_team.shared.fencing import StaleFencingTokenError

    prov = _MinimalProv(tmp_path)
    prov._state.put("a", {"x": 1}, fencing_token=5)

    calls: list[int] = []

    class _StaleProv(BaseToolProvisioner):
        tool_name = "stale"

        def __init__(self) -> None:
            self._state = prov._state

        def provision(self, agent_id, config, credentials, fencing_token=None):
            def _create(_r):
                calls.append(1)
                return (["read"], {"x": 2})

            return self.run_idempotent(
                agent_id, credentials=credentials, create=_create, fencing_token=fencing_token
            )

        def verify_access(self, agent_id):
            return self._make_verification(passed=True, actual_permissions=[])

        def deprovision(self, agent_id):
            from agent_team_studio.agent_provisioning_team.models import DeprovisionResult

            return DeprovisionResult(tool_name=self.tool_name, success=True)

    with pytest.raises(StaleFencingTokenError):
        _StaleProv().provision("a", {}, GeneratedCredentials(tool_name="x"), fencing_token=4)

    assert calls == []  # create() (the real side effect) must never have run


def test_run_idempotent_does_not_swallow_stale_fencing_token_error(tmp_path: Path) -> None:
    """StaleFencingTokenError must propagate, not be converted into an
    ordinary success=False ToolProvisionResult by the generic catch-all."""
    from agent_team_studio.agent_provisioning_team.shared.fencing import StaleFencingTokenError

    prov = _MinimalProv(tmp_path)
    prov._state.put("a", {"x": 1}, fencing_token=5)

    with pytest.raises(StaleFencingTokenError):
        prov.provision("a", {}, GeneratedCredentials(tool_name="x"), fencing_token=4)


def test_run_idempotent_accepts_and_stamps_fencing_token(tmp_path: Path) -> None:
    prov = _MinimalProv(tmp_path)
    out = prov.provision("a", {}, GeneratedCredentials(tool_name="x"), fencing_token=5)
    assert out.success is True
    assert prov._state._load()["a"]["fencing_token"] == 5


def test_run_idempotent_calls_on_persist_failure_when_state_put_raises(tmp_path: Path) -> None:
    """A resource `create` returns must not leak when persisting its state fails.

    If `state.put` raises after `create` succeeds, the store never recorded the
    resource, so the normal state-lookup-based `deprovision(agent_id)` path has
    nothing to find it by. `on_persist_failure` gets the just-returned `details`
    directly so a provisioner can tear the resource down by name instead.
    """
    cleanup_calls = []

    class _PersistFailProv(BaseToolProvisioner):
        tool_name = "persistfail"

        def __init__(self) -> None:
            self._state = ProvisionerStateStore("persistfail_prov", storage_dir=tmp_path)

        def provision(self, agent_id, config, credentials):
            return self.run_idempotent(
                agent_id,
                credentials=credentials,
                create=lambda _r: (["read"], {"resource": "x"}),
                on_persist_failure=cleanup_calls.append,
            )

        def verify_access(self, agent_id):
            return self._make_verification(passed=True, actual_permissions=[])

        def deprovision(self, agent_id):
            from agent_team_studio.agent_provisioning_team.models import DeprovisionResult

            return DeprovisionResult(tool_name=self.tool_name, success=True)

    prov = _PersistFailProv()

    def _boom(agent_id, value, fencing_token=None):
        raise OSError("disk full")

    prov._state.put = _boom

    out = prov.provision("a", {}, GeneratedCredentials(tool_name="x"))
    assert out.success is False
    assert "disk full" in out.error
    assert cleanup_calls == [{"resource": "x"}]


def test_run_idempotent_on_persist_failure_exception_is_swallowed(tmp_path: Path, caplog) -> None:
    """A raising `on_persist_failure` cleanup must not mask the original error."""

    class _PersistFailProv(BaseToolProvisioner):
        tool_name = "persistfail2"

        def __init__(self) -> None:
            self._state = ProvisionerStateStore("persistfail2_prov", storage_dir=tmp_path)

        def provision(self, agent_id, config, credentials):
            def _hook_boom(details):
                raise RuntimeError("cleanup also failed")

            return self.run_idempotent(
                agent_id,
                credentials=credentials,
                create=lambda _r: (["read"], {"resource": "x"}),
                on_persist_failure=_hook_boom,
            )

        def verify_access(self, agent_id):
            return self._make_verification(passed=True, actual_permissions=[])

        def deprovision(self, agent_id):
            from agent_team_studio.agent_provisioning_team.models import DeprovisionResult

            return DeprovisionResult(tool_name=self.tool_name, success=True)

    prov = _PersistFailProv()

    def _boom(agent_id, value, fencing_token=None):
        raise OSError("disk full")

    prov._state.put = _boom

    with caplog.at_level(logging.ERROR):
        out = prov.provision("a", {}, GeneratedCredentials(tool_name="x"))

    assert out.success is False
    assert "disk full" in out.error
    assert "cleanup also failed" not in out.error


# ---------------------------------------------------------------------------
# docker_provisioner.py
# ---------------------------------------------------------------------------


def _docker_run_success(*args, **kwargs):
    return SimpleNamespace(returncode=0, stdout="abc123def456789012\n", stderr="")


def test_docker_provisioner_provision_success(tmp_path: Path, monkeypatch) -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    prov = DockerProvisionerTool(workspace_base=str(tmp_path / "ws"))
    prov._state = ProvisionerStateStore("docker_provisioner", storage_dir=tmp_path)

    with patch("subprocess.run", side_effect=_docker_run_success):
        result = prov.provision(
            "agent-1",
            {"base_image": "python:3.11", "expose_ssh": True, "environment": {"FOO": "bar"}},
            GeneratedCredentials(tool_name="docker"),
        )

    assert result.success is True
    assert result.details["container_id"].startswith("abc123")
    assert result.details["status"] == "running"


def test_docker_provisioner_provision_returns_error_on_nonzero(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    prov = DockerProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("docker_provisioner", storage_dir=tmp_path)

    def _fail(*a, **kw):
        return SimpleNamespace(returncode=1, stdout="", stderr="something bad")

    with patch("subprocess.run", side_effect=_fail):
        result = prov.provision("agent-1", {}, GeneratedCredentials(tool_name="docker"))

    assert result.success is False
    assert "Docker run failed" in result.error or "something bad" in result.error


def _docker_cmd_stub(record, run=None, inspect=None, rm=None):
    """subprocess.run stub keyed on the docker subcommand; records every cmd."""

    def _respond(cmd, *a, **kw):
        record.append(cmd)
        if cmd[:2] == ["docker", "inspect"]:
            return inspect or SimpleNamespace(
                returncode=1, stdout="", stderr="Error: No such object"
            )
        if cmd[:2] == ["docker", "run"]:
            outcome = run or SimpleNamespace(returncode=125, stdout="", stderr="run failed")
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        if cmd[:3] == ["docker", "rm", "-f"]:
            return rm or SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return _respond


def test_docker_provisioner_removes_partial_container_on_failed_run(tmp_path: Path) -> None:
    """A failed/timed-out `docker run` triggers best-effort removal by name.

    No state row is written on failure, so deprovision-by-state could never find
    such a container; without this cleanup the leftover name would block every
    future provision for the agent. The pre-run probe reported the name absent,
    so the removal is provably scoped to this attempt's container.
    """
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    prov = DockerProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("docker_provisioner", storage_dir=tmp_path)

    calls = []
    with patch("subprocess.run", side_effect=_docker_cmd_stub(calls)):
        result = prov.provision("agent-1", {}, GeneratedCredentials(tool_name="docker"))

    assert result.success is False
    assert ["docker", "rm", "-f", "agent-agent-1"] in calls

    calls.clear()
    timeout = _subprocess.TimeoutExpired(cmd=["docker", "run"], timeout=120)
    with patch("subprocess.run", side_effect=_docker_cmd_stub(calls, run=timeout)):
        result = prov.provision("agent-2", {}, GeneratedCredentials(tool_name="docker"))

    assert result.success is False
    assert ["docker", "rm", "-f", "agent-agent-2"] in calls


def test_docker_provisioner_preserves_preexisting_container_on_failed_run(
    tmp_path: Path,
) -> None:
    """A run failing against a PRE-EXISTING same-named container must not remove it.

    When the state row is lost but the container lives on, `docker run` fails on
    the name conflict; deleting the container by name would destroy a healthy
    agent, so the cleanup only fires when the pre-run probe reported the name
    absent. An unknown probe result (daemon error) is also conservative: no rm.
    """
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    prov = DockerProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("docker_provisioner", storage_dir=tmp_path)

    calls = []
    exists = SimpleNamespace(returncode=0, stdout="abc\n", stderr="")
    conflict = SimpleNamespace(returncode=125, stdout="", stderr="name is already in use")
    with patch("subprocess.run", side_effect=_docker_cmd_stub(calls, inspect=exists, run=conflict)):
        result = prov.provision("agent-1", {}, GeneratedCredentials(tool_name="docker"))

    assert result.success is False
    assert not any(cmd[:3] == ["docker", "rm", "-f"] for cmd in calls)

    calls.clear()
    probe_error = SimpleNamespace(returncode=1, stdout="", stderr="daemon unavailable")
    with patch(
        "subprocess.run", side_effect=_docker_cmd_stub(calls, inspect=probe_error, run=conflict)
    ):
        result = prov.provision("agent-1", {}, GeneratedCredentials(tool_name="docker"))

    assert result.success is False
    assert not any(cmd[:3] == ["docker", "rm", "-f"] for cmd in calls)


def test_docker_provisioner_skips_removal_on_name_conflict(tmp_path: Path) -> None:
    """A name-conflict run failure must never remove the conflicting container.

    Two concurrent setups can both probe the name as absent; the loser's `docker
    run` fails with "already in use" — which proves its run created nothing and
    the container is the winner's. Removing by name would destroy the winner's
    live container just before it registers.
    """
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    prov = DockerProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("docker_provisioner", storage_dir=tmp_path)

    calls = []
    conflict = SimpleNamespace(
        returncode=125,
        stdout="",
        stderr='Conflict. The container name "/agent-agent-1" is already in use by container "abc"',
    )
    with patch("subprocess.run", side_effect=_docker_cmd_stub(calls, run=conflict)):
        result = prov.provision("agent-1", {}, GeneratedCredentials(tool_name="docker"))

    assert result.success is False
    assert not any(cmd[:3] == ["docker", "rm", "-f"] for cmd in calls)


def test_docker_provisioner_removes_container_on_port_bind_failure(tmp_path: Path) -> None:
    """A port-bind failure ('address already in use') is not a name conflict.

    Docker's port-bind error also contains the substring "already in use" but
    is unrelated to the container-name conflict; matching that broad substring
    would suppress cleanup and leak the container docker created before the
    bind step failed. Only the daemon's actual name-conflict wording should
    suppress removal.
    """
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    prov = DockerProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("docker_provisioner", storage_dir=tmp_path)

    calls = []
    bind_failure = SimpleNamespace(
        returncode=125,
        stdout="",
        stderr="failed to bind host port for 0.0.0.0:8000: address already in use",
    )
    with patch("subprocess.run", side_effect=_docker_cmd_stub(calls, run=bind_failure)):
        result = prov.provision("agent-1", {}, GeneratedCredentials(tool_name="docker"))

    assert result.success is False
    assert ["docker", "rm", "-f", "agent-agent-1"] in calls


def test_docker_provisioner_cleans_up_on_post_launch_exception(tmp_path: Path) -> None:
    """A post-launch exception (e.g. output decoding) still triggers cleanup.

    subprocess.run can raise after the daemon created the container; only
    handling TimeoutExpired would leave that container untracked and its name
    blocking future provisions.
    """
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    prov = DockerProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("docker_provisioner", storage_dir=tmp_path)

    calls = []
    decode_error = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
    with patch("subprocess.run", side_effect=_docker_cmd_stub(calls, run=decode_error)):
        result = prov.provision("agent-1", {}, GeneratedCredentials(tool_name="docker"))

    assert result.success is False
    assert ["docker", "rm", "-f", "agent-agent-1"] in calls


def test_docker_provisioner_logs_failed_best_effort_removal(tmp_path: Path, caplog) -> None:
    """A best-effort `docker rm -f` that exits nonzero must be logged, not silent."""
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    prov = DockerProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("docker_provisioner", storage_dir=tmp_path)

    calls = []
    rm_fail = SimpleNamespace(returncode=1, stdout="", stderr="cannot remove: device busy")
    with caplog.at_level(logging.ERROR):
        with patch("subprocess.run", side_effect=_docker_cmd_stub(calls, rm=rm_fail)):
            result = prov.provision("agent-1", {}, GeneratedCredentials(tool_name="docker"))

    assert result.success is False
    assert ["docker", "rm", "-f", "agent-agent-1"] in calls
    assert "device busy" in caplog.text


def test_docker_provisioner_removes_container_when_state_persist_fails(
    tmp_path: Path,
) -> None:
    """A successfully created container must not leak when persisting state fails.

    `docker run` can succeed and then the idempotency-store write can still
    raise (e.g. disk full): the store never recorded the container, so
    `deprovision(agent_id)`'s state-lookup path can't find it afterward — this
    exercises the `on_persist_failure` wiring that removes it directly by the
    name `_do_provision` already returned.
    """
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    prov = DockerProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("docker_provisioner", storage_dir=tmp_path)

    def _put_boom(agent_id, value):
        raise OSError("disk full")

    prov._state.put = _put_boom

    calls = []
    success = SimpleNamespace(returncode=0, stdout="abc123def456789012\n", stderr="")
    with patch("subprocess.run", side_effect=_docker_cmd_stub(calls, run=success)):
        result = prov.provision("agent-1", {}, GeneratedCredentials(tool_name="docker"))

    assert result.success is False
    assert ["docker", "rm", "-f", "agent-agent-1"] in calls
    assert prov._state.get("agent-1") is None


def test_docker_provisioner_is_idempotent_on_existing_state(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    prov = DockerProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("docker_provisioner", storage_dir=tmp_path)

    with patch("subprocess.run", side_effect=_docker_run_success):
        first = prov.provision("a1", {}, GeneratedCredentials(tool_name="docker"))
        assert first.success

    # Second call reuses state; _on_reuse's liveness probe must see the
    # container as confirmed-alive here, or it (correctly) treats the reuse
    # as stale and refuses it.
    exists = SimpleNamespace(returncode=0, stdout="abc\n", stderr="")
    with patch("subprocess.run", side_effect=_docker_cmd_stub([], inspect=exists)):
        second = prov.provision("a1", {}, GeneratedCredentials(tool_name="docker"))
    assert second.success
    assert second.details.get("reused") is True


def test_docker_verify_access_no_state(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    prov = DockerProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("docker_provisioner", storage_dir=tmp_path)

    v = prov.verify_access("missing-agent")
    assert v.passed is False
    assert "No container" in v.errors[0]


def test_docker_verify_access_success(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    prov = DockerProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("docker_provisioner", storage_dir=tmp_path)
    prov._state.put("a1", {"container_name": "c1"})

    with patch(
        "subprocess.run",
        return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
    ):
        v = prov.verify_access("a1")

    assert v.passed is True


def test_docker_verify_access_failure(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    prov = DockerProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("docker_provisioner", storage_dir=tmp_path)
    prov._state.put("a1", {"container_name": "c1"})

    with patch(
        "subprocess.run",
        return_value=SimpleNamespace(returncode=1, stdout="", stderr="bad"),
    ):
        v = prov.verify_access("a1")

    assert v.passed is False


def test_docker_verify_access_exception(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    prov = DockerProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("docker_provisioner", storage_dir=tmp_path)
    prov._state.put("a1", {"container_name": "c1"})

    with patch("subprocess.run", side_effect=OSError("kaboom")):
        v = prov.verify_access("a1")

    assert v.passed is False
    assert "kaboom" in v.errors[0]


def test_docker_deprovision_no_state(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    prov = DockerProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("docker_provisioner", storage_dir=tmp_path)

    out = prov.deprovision("nobody")
    assert out.success is True
    assert "No container" in out.details["message"]


def test_docker_deprovision_with_state(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    prov = DockerProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("docker_provisioner", storage_dir=tmp_path)
    prov._state.put("a1", {"container_name": "c1"})

    with patch(
        "subprocess.run",
        return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
    ):
        out = prov.deprovision("a1")

    assert out.success is True
    assert out.details["container_removed"] == "c1"


def test_docker_deprovision_reports_failure_and_keeps_state_on_rm_error(
    tmp_path: Path,
) -> None:
    """A `docker rm -f` that exits nonzero must report failure and preserve state.

    Deleting the state row while the container survives would leave it
    untracked — unreachable by any later deprovision-by-agent-id — and its name
    would block every future provision.
    """
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    prov = DockerProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("docker_provisioner", storage_dir=tmp_path)
    prov._state.put("a1", {"container_name": "c1"})

    rm_fail = SimpleNamespace(returncode=1, stdout="", stderr="cannot remove: device busy")
    with patch("subprocess.run", return_value=rm_fail):
        out = prov.deprovision("a1")

    assert out.success is False
    assert "device busy" in out.error
    assert prov._state.get("a1") is not None


def test_docker_deprovision_confirms_removal_after_ambiguous_rm_failure(
    tmp_path: Path,
) -> None:
    """An ambiguous `rm` failure whose follow-up probe confirms absence still clears state.

    The daemon can remove the container but the CLI still reports failure (the
    response or connection was lost afterward). Treating that as a genuine
    failure would strand the state row pointing at a container that is
    actually gone, which the next provision() would blindly reuse and register
    as a running environment. A follow-up existence probe disambiguates.
    """
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    prov = DockerProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("docker_provisioner", storage_dir=tmp_path)
    prov._state.put("a1", {"container_name": "c1"})

    def _respond(cmd, *a, **kw):
        if cmd[:2] == ["docker", "rm"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="connection reset by peer")
        if cmd[:2] == ["docker", "inspect"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="Error: No such object: c1")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=_respond):
        out = prov.deprovision("a1")

    assert out.success is True
    assert prov._state.get("a1") is None


def test_docker_deprovision_ambiguous_rm_failure_with_unknown_followup_preserves_state(
    tmp_path: Path,
) -> None:
    """An ambiguous `rm` failure whose follow-up probe is ALSO ambiguous preserves state.

    Only a CONFIRMED absence (the daemon's specific missing-container response)
    justifies clearing state; an unknown result from the follow-up probe (e.g.
    the daemon is unreachable) must not be treated as proof of removal.
    """
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    prov = DockerProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("docker_provisioner", storage_dir=tmp_path)
    prov._state.put("a1", {"container_name": "c1"})

    def _respond(cmd, *a, **kw):
        if cmd[:2] == ["docker", "rm"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="connection reset by peer")
        if cmd[:2] == ["docker", "inspect"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="daemon unavailable")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=_respond):
        out = prov.deprovision("a1")

    assert out.success is False
    assert prov._state.get("a1") is not None


def test_docker_deprovision_infrastructure_error_is_not_absence(tmp_path: Path) -> None:
    """'no such file or directory' (daemon/storage error) must not pass as absence.

    Only the daemon's specific missing-container response proves the container
    is gone; a broad "no such" match would delete the state row on an
    infrastructure error while the container may still be alive.
    """
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    prov = DockerProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("docker_provisioner", storage_dir=tmp_path)
    prov._state.put("a1", {"container_name": "c1"})

    infra = SimpleNamespace(
        returncode=1,
        stdout="",
        stderr="error during connect: open //./pipe/docker: no such file or directory",
    )
    with patch("subprocess.run", return_value=infra):
        out = prov.deprovision("a1")

    assert out.success is False
    assert prov._state.get("a1") is not None


def test_docker_deprovision_treats_already_absent_container_as_removed(
    tmp_path: Path,
) -> None:
    """`docker rm -f` reporting 'No such container' counts as a completed removal."""
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    prov = DockerProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("docker_provisioner", storage_dir=tmp_path)
    prov._state.put("a1", {"container_name": "c1"})

    gone = SimpleNamespace(returncode=1, stdout="", stderr="Error: No such container: c1")
    with patch("subprocess.run", return_value=gone):
        out = prov.deprovision("a1")

    assert out.success is True
    assert prov._state.get("a1") is None


def test_docker_deprovision_handles_exception(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    prov = DockerProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("docker_provisioner", storage_dir=tmp_path)
    prov._state.put("a1", {"container_name": "c1"})

    with patch("subprocess.run", side_effect=RuntimeError("daemon down")):
        out = prov.deprovision("a1")

    assert out.success is False
    assert "daemon down" in out.error


def test_docker_deprovision_rejects_stale_token_before_touching_docker(tmp_path: Path) -> None:
    """The preflight check must reject BEFORE the real `docker stop`/`docker
    rm` subprocess calls -- not just before the final state.delete()."""
    from agent_team_studio.agent_provisioning_team.shared.fencing import StaleFencingTokenError
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    prov = DockerProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("docker_provisioner", storage_dir=tmp_path)
    prov._state.put("a1", {"container_name": "c1"}, fencing_token=5)

    with patch("subprocess.run") as mock_run:
        with pytest.raises(StaleFencingTokenError):
            prov.deprovision("a1", fencing_token=4)
        mock_run.assert_not_called()

    # The container record must still be present -- nothing was deleted.
    assert prov._state.get("a1") is not None


def test_deprovision_propagates_stale_token_from_trailing_delete(tmp_path: Path) -> None:
    """A StaleFencingTokenError raised by the FINAL _state.delete (token went
    stale mid-teardown) must propagate as the non-retryable ownership error, not
    be swallowed by the broad `except Exception -> success=False` into a soft
    failure — matching run_idempotent's re-raise and the deprovision contract.

    Covers the generic/git/docker provisioners (redis/postgres early-return in a
    sandbox without those packages); all five share the identical re-raise guard.
    """
    from agent_team_studio.agent_provisioning_team.shared.fencing import StaleFencingTokenError
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )
    from agent_team_studio.agent_provisioning_team.tool_agents.generic_provisioner import (
        GenericProvisionerTool,
    )
    from agent_team_studio.agent_provisioning_team.tool_agents.git_provisioner import (
        GitProvisionerTool,
    )

    def _staled_state(resource: str) -> MagicMock:
        fake = MagicMock()
        fake.check_fencing_token.return_value = None  # entry preflight passes
        fake.get.return_value = {"tool_name": "t", "workspace_path": str(tmp_path / "nope")}
        fake.delete.side_effect = StaleFencingTokenError("a1", resource, 4, 5)
        return fake

    generic = GenericProvisionerTool()
    generic._state = _staled_state("generic_provisioner")
    with pytest.raises(StaleFencingTokenError):
        generic.deprovision("a1", fencing_token=4)

    git = GitProvisionerTool()
    git._state = _staled_state("git_provisioner")
    with pytest.raises(StaleFencingTokenError):
        git.deprovision("a1", fencing_token=4)

    docker = DockerProvisionerTool(workspace_base=str(tmp_path))
    docker._state = _staled_state("docker_provisioner")
    docker._state.get.return_value = {"container_name": "c1"}
    with patch(
        "subprocess.run",
        return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
    ):
        with pytest.raises(StaleFencingTokenError):
            docker.deprovision("a1", fencing_token=4)


def test_docker_deprovision_accepts_current_token(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    prov = DockerProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("docker_provisioner", storage_dir=tmp_path)
    prov._state.put("a1", {"container_name": "c1"}, fencing_token=5)

    with patch(
        "subprocess.run",
        return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
    ):
        out = prov.deprovision("a1", fencing_token=5)

    assert out.success is True
    assert prov._state.get("a1") is None


def test_docker_allocate_port_is_deterministic() -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    prov = DockerProvisionerTool()
    p1 = prov._allocate_port("agent-a")
    p2 = prov._allocate_port("agent-a")
    assert p1 == p2
    assert 22000 <= p1 < 23000


def test_docker_get_container_info(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    prov = DockerProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("docker_provisioner", storage_dir=tmp_path)
    prov._state.put("a1", {"container_name": "c1"})

    info = prov.get_container_info("a1")
    assert info["container_name"] == "c1"
    assert prov.get_container_info("missing") is None


def test_docker_exec_in_container_no_state(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    prov = DockerProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("docker_provisioner", storage_dir=tmp_path)

    rc, out, err = prov.exec_in_container("missing", ["ls"])
    assert rc == 1
    assert "No container" in err


def test_docker_exec_in_container_runs(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    prov = DockerProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("docker_provisioner", storage_dir=tmp_path)
    prov._state.put("a1", {"container_name": "c1"})

    with patch(
        "subprocess.run",
        return_value=SimpleNamespace(returncode=0, stdout="hi\n", stderr=""),
    ):
        rc, out, err = prov.exec_in_container("a1", ["echo", "hi"])
    assert rc == 0
    assert out == "hi\n"


def test_docker_exec_in_container_timeout(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    prov = DockerProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("docker_provisioner", storage_dir=tmp_path)
    prov._state.put("a1", {"container_name": "c1"})

    with patch(
        "subprocess.run",
        side_effect=_subprocess.TimeoutExpired(cmd="docker exec", timeout=1),
    ):
        rc, out, err = prov.exec_in_container("a1", ["sleep", "100"])
    assert rc == 1
    assert "timed out" in err


def test_docker_exec_in_container_handles_exception(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    prov = DockerProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("docker_provisioner", storage_dir=tmp_path)
    prov._state.put("a1", {"container_name": "c1"})

    with patch("subprocess.run", side_effect=OSError("boom")):
        rc, out, err = prov.exec_in_container("a1", ["x"])
    assert rc == 1
    assert "boom" in err


def test_docker_on_reuse_populates_credentials(tmp_path: Path) -> None:
    """Cover the _on_reuse path: stored state + fresh creds."""
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    prov = DockerProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("docker_provisioner", storage_dir=tmp_path)
    prov._state.put(
        "a1",
        {
            "container_id": "abcd",
            "container_name": "c1",
            "workspace_path": "/ws",
            "status": "running",
        },
    )

    creds = GeneratedCredentials(tool_name="docker")
    exists = SimpleNamespace(returncode=0, stdout="abc\n", stderr="")
    with patch("subprocess.run", side_effect=_docker_cmd_stub([], inspect=exists)):
        result = prov.provision("a1", {}, creds)
    assert result.success is True
    assert creds.extra["container_id"] == "abcd"


def test_docker_on_reuse_clears_stale_state_when_container_confirmed_gone(
    tmp_path: Path,
) -> None:
    """A stored container that the daemon confirms is gone must not be reused.

    Trusting a stale idempotency row would register a "running" environment
    backed by nothing. The row is deleted so a retried provision() recreates
    the container fresh instead of hitting the same stale row forever.
    """
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    prov = DockerProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("docker_provisioner", storage_dir=tmp_path)
    prov._state.put(
        "a1",
        {"container_id": "abcd", "container_name": "c1", "workspace_path": "/ws"},
    )

    absent = SimpleNamespace(returncode=1, stdout="", stderr="Error: No such object")
    with patch("subprocess.run", side_effect=_docker_cmd_stub([], inspect=absent)):
        result = prov.provision("a1", {}, GeneratedCredentials(tool_name="docker"))

    assert result.success is False
    assert prov._state.get("a1") is None


def test_docker_on_reuse_proceeds_when_existence_unknown(tmp_path: Path) -> None:
    """An unreachable daemon during the reuse probe must not block reuse.

    Unknown is not the same as confirmed-absent — treating it as absence would
    risk tearing down tracking for a container that is actually still alive.
    """
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    prov = DockerProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("docker_provisioner", storage_dir=tmp_path)
    prov._state.put(
        "a1",
        {"container_id": "abcd", "container_name": "c1", "workspace_path": "/ws"},
    )

    unreachable = SimpleNamespace(returncode=1, stdout="", stderr="daemon unavailable")
    with patch("subprocess.run", side_effect=_docker_cmd_stub([], inspect=unreachable)):
        result = prov.provision("a1", {}, GeneratedCredentials(tool_name="docker"))

    assert result.success is True
    assert result.details.get("reused") is True
    assert prov._state.get("a1") is not None


def test_docker_provisioner_stamps_job_id_label_when_config_key_present(tmp_path: Path) -> None:
    """A job_id present in config under JOB_ID_CONFIG_KEY is stamped as a
    khala.job_id Docker label -- the attempt-scoped identity marker
    compensate_activity/check_existing_environment_activity read back to
    disambiguate a self-leaked container from a pre-existing one.
    """
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        JOB_ID_CONFIG_KEY,
        DockerProvisionerTool,
    )

    prov = DockerProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("docker_provisioner", storage_dir=tmp_path)

    calls = []
    success = SimpleNamespace(returncode=0, stdout="abc123def456789012\n", stderr="")
    with patch("subprocess.run", side_effect=_docker_cmd_stub(calls, run=success)):
        result = prov.provision(
            "agent-1", {JOB_ID_CONFIG_KEY: "job-42"}, GeneratedCredentials(tool_name="docker")
        )

    assert result.success is True
    run_cmd = next(c for c in calls if c[:2] == ["docker", "run"])
    assert "--label" in run_cmd
    assert run_cmd[run_cmd.index("--label") + 1] == "khala.job_id=job-42"


def test_docker_provisioner_omits_label_when_no_job_id_in_config(tmp_path: Path) -> None:
    """No JOB_ID_CONFIG_KEY in config means no --label flag at all -- keeps
    non-Temporal / job_id-less callers behaving exactly as before this
    labeling primitive existed.
    """
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    prov = DockerProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("docker_provisioner", storage_dir=tmp_path)

    calls = []
    success = SimpleNamespace(returncode=0, stdout="abc123def456789012\n", stderr="")
    with patch("subprocess.run", side_effect=_docker_cmd_stub(calls, run=success)):
        result = prov.provision("agent-1", {}, GeneratedCredentials(tool_name="docker"))

    assert result.success is True
    run_cmd = next(c for c in calls if c[:2] == ["docker", "run"])
    assert "--label" not in run_cmd


def test_inspect_existence_and_owner_returns_exists_and_label() -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    labeled = SimpleNamespace(returncode=0, stdout="abc123|||job-42\n", stderr="")
    with patch("subprocess.run", return_value=labeled):
        assert DockerProvisionerTool._inspect_existence_and_owner("agent-a1") == (True, "job-42")


def test_inspect_existence_and_owner_returns_exists_and_none_when_unlabeled() -> None:
    """Docker's Go-template `index` on a missing label key yields an empty
    string, not an error -- must be treated the same as "no ownership signal".
    """
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    unlabeled = SimpleNamespace(returncode=0, stdout="abc123|||\n", stderr="")
    with patch("subprocess.run", return_value=unlabeled):
        assert DockerProvisionerTool._inspect_existence_and_owner("agent-a1") == (True, None)


def test_inspect_existence_and_owner_returns_absent() -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    absent = SimpleNamespace(returncode=1, stdout="", stderr="Error: No such object")
    with patch("subprocess.run", return_value=absent):
        assert DockerProvisionerTool._inspect_existence_and_owner("agent-a1") == (False, None)


def test_inspect_existence_and_owner_returns_unknown_on_probe_error() -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    with patch("subprocess.run", side_effect=OSError("daemon unreachable")):
        assert DockerProvisionerTool._inspect_existence_and_owner("agent-a1") == (None, None)


def test_inspect_existence_and_owner_returns_unknown_on_ambiguous_failure() -> None:
    """A nonzero exit that isn't the daemon's specific absence response is
    inconclusive, not confirmed-absent (e.g. a transient daemon error)."""
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    ambiguous = SimpleNamespace(returncode=1, stdout="", stderr="daemon unavailable")
    with patch("subprocess.run", return_value=ambiguous):
        assert DockerProvisionerTool._inspect_existence_and_owner("agent-a1") == (None, None)


def test_is_pre_existing_false_when_confirmed_absent() -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    with patch.object(
        DockerProvisionerTool, "_inspect_existence_and_owner", return_value=(False, None)
    ) as mock_probe:
        assert DockerProvisionerTool.is_pre_existing("a1", "job-42") is False

    mock_probe.assert_called_once_with("agent-a1")


def test_is_pre_existing_false_when_label_matches_job_id() -> None:
    """Confirmed self-leak: the container's own label matches this run's
    job_id, so it cannot predate this run."""
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    with patch.object(
        DockerProvisionerTool, "_inspect_existence_and_owner", return_value=(True, "job-42")
    ):
        assert DockerProvisionerTool.is_pre_existing("a1", "job-42") is False


def test_is_pre_existing_true_when_label_is_foreign_job(caplog) -> None:
    """A different job's label stays protected, same as no label -- and is
    logged informationally rather than as an alarming/unexpected finding."""
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    with (
        patch.object(
            DockerProvisionerTool, "_inspect_existence_and_owner", return_value=(True, "job-99")
        ),
        caplog.at_level("INFO"),
    ):
        assert DockerProvisionerTool.is_pre_existing("a1", "job-42") is True

    assert "job-99" in caplog.text
    assert "job-42" in caplog.text


def test_is_pre_existing_true_when_no_label_and_job_id_given() -> None:
    """The container exists but carries no label at all (e.g. predates this
    labeling primitive) -- must stay conservative even though job_id was
    given to compare against."""
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    with patch.object(
        DockerProvisionerTool, "_inspect_existence_and_owner", return_value=(True, None)
    ):
        assert DockerProvisionerTool.is_pre_existing("a1", "job-42") is True


def test_is_pre_existing_true_when_job_id_not_given() -> None:
    """No job_id to compare against at all -- unchanged conservative fallback,
    matching behavior from before this labeling primitive existed."""
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    with patch.object(
        DockerProvisionerTool, "_inspect_existence_and_owner", return_value=(True, "job-1")
    ):
        assert DockerProvisionerTool.is_pre_existing("a1", None) is True


def test_is_pre_existing_true_when_inconclusive() -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    with patch.object(
        DockerProvisionerTool, "_inspect_existence_and_owner", return_value=(None, None)
    ):
        assert DockerProvisionerTool.is_pre_existing("a1", "job-42") is True


def test_docker_provisioner_normalizes_job_id_whitespace_before_stamping(tmp_path: Path) -> None:
    """job_id is stripped before being stamped, so it can never disagree with
    the also-stripped value _inspect_existence_and_owner reads back."""
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        JOB_ID_CONFIG_KEY,
        DockerProvisionerTool,
    )

    prov = DockerProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("docker_provisioner", storage_dir=tmp_path)

    calls = []
    success = SimpleNamespace(returncode=0, stdout="abc123def456789012\n", stderr="")
    with patch("subprocess.run", side_effect=_docker_cmd_stub(calls, run=success)):
        result = prov.provision(
            "agent-1", {JOB_ID_CONFIG_KEY: "  job-42  "}, GeneratedCredentials(tool_name="docker")
        )

    assert result.success is True
    run_cmd = next(c for c in calls if c[:2] == ["docker", "run"])
    assert run_cmd[run_cmd.index("--label") + 1] == "khala.job_id=job-42"


def test_docker_provisioner_reclaims_container_on_name_conflict_from_same_job_id(
    tmp_path: Path,
) -> None:
    """A container blocking the name, labeled with THIS SAME job_id, is a
    self-leak from an earlier attempt (e.g. local idempotency state lost
    after a retried activity) -- reclaimed before docker run, so a retry of
    the same job_id can converge cleanly instead of hard-failing."""
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        JOB_ID_CONFIG_KEY,
        DockerProvisionerTool,
    )

    prov = DockerProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("docker_provisioner", storage_dir=tmp_path)

    calls = []
    success = SimpleNamespace(returncode=0, stdout="abc123def456789012\n", stderr="")

    def _inspect_existing_self_owned(cmd, *a, **kw):
        calls.append(cmd)
        if cmd[:2] == ["docker", "inspect"]:
            return SimpleNamespace(returncode=0, stdout="oldid|||job-42\n", stderr="")
        if cmd[:2] == ["docker", "run"]:
            return success
        if cmd[:3] == ["docker", "rm", "-f"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=_inspect_existing_self_owned):
        result = prov.provision(
            "agent-1", {JOB_ID_CONFIG_KEY: "job-42"}, GeneratedCredentials(tool_name="docker")
        )

    assert result.success is True
    assert ["docker", "rm", "-f", "agent-agent-1"] in calls


def test_docker_provisioner_does_not_reclaim_container_from_different_job_id(
    tmp_path: Path,
) -> None:
    """A container blocking the name labeled with a DIFFERENT job's job_id
    must not be proactively removed -- only a confirmed self-leak is safe
    to reclaim before docker run."""
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        JOB_ID_CONFIG_KEY,
        DockerProvisionerTool,
    )

    prov = DockerProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("docker_provisioner", storage_dir=tmp_path)

    calls = []
    conflict = SimpleNamespace(returncode=125, stdout="", stderr="already in use by container")

    def _inspect_existing_foreign_owned(cmd, *a, **kw):
        calls.append(cmd)
        if cmd[:2] == ["docker", "inspect"]:
            return SimpleNamespace(returncode=0, stdout="oldid|||some-other-job\n", stderr="")
        if cmd[:2] == ["docker", "run"]:
            return conflict
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=_inspect_existing_foreign_owned):
        result = prov.provision(
            "agent-1", {JOB_ID_CONFIG_KEY: "job-42"}, GeneratedCredentials(tool_name="docker")
        )

    assert result.success is False
    assert ["docker", "rm", "-f", "agent-agent-1"] not in calls


# ---------------------------------------------------------------------------
# generic_provisioner.py
# ---------------------------------------------------------------------------


def test_generic_provisioner_provision_success(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents.generic_provisioner import (
        GenericProvisionerTool,
    )

    tool = GenericProvisionerTool(tool_name="mytool")
    tool._state = ProvisionerStateStore("generic_mytool_provisioner", storage_dir=tmp_path)

    creds = GeneratedCredentials(tool_name="mytool")
    out = tool.provision("a1", {"permissions": ["read", "write"]}, creds)
    assert out.success is True
    assert "read" in out.permissions
    assert creds.extra["tool_name"] == "mytool"


def test_generic_provisioner_default_permissions(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents.generic_provisioner import (
        GenericProvisionerTool,
    )

    tool = GenericProvisionerTool(tool_name="t")
    tool._state = ProvisionerStateStore("generic_t_provisioner", storage_dir=tmp_path)

    out = tool.provision("a1", {}, GeneratedCredentials(tool_name="t"))
    assert out.permissions == ["all"]


def test_generic_provisioner_verify_no_state(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents.generic_provisioner import (
        GenericProvisionerTool,
    )

    tool = GenericProvisionerTool(tool_name="t")
    tool._state = ProvisionerStateStore("generic_t_provisioner", storage_dir=tmp_path)

    v = tool.verify_access("missing")
    assert v.passed is False
    assert "No provisioning" in v.errors[0]


def test_generic_provisioner_verify_with_state(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents.generic_provisioner import (
        GenericProvisionerTool,
    )

    tool = GenericProvisionerTool(tool_name="t")
    tool._state = ProvisionerStateStore("generic_t_provisioner", storage_dir=tmp_path)
    tool._state.put("a1", {"permissions": ["read"]})

    v = tool.verify_access("a1")
    assert v.passed is True
    assert v.actual_permissions == ["read"]


def test_generic_provisioner_deprovision_no_state(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents.generic_provisioner import (
        GenericProvisionerTool,
    )

    tool = GenericProvisionerTool(tool_name="t")
    tool._state = ProvisionerStateStore("generic_t_provisioner", storage_dir=tmp_path)

    out = tool.deprovision("missing")
    assert out.success is True
    assert "No provisioning" in out.details["message"]


def test_generic_provisioner_deprovision_with_state(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents.generic_provisioner import (
        GenericProvisionerTool,
    )

    tool = GenericProvisionerTool(tool_name="t")
    tool._state = ProvisionerStateStore("generic_t_provisioner", storage_dir=tmp_path)
    tool._state.put("a1", {"permissions": ["read"]})

    out = tool.deprovision("a1")
    assert out.success is True
    assert out.details["deprovisioned"] is True


def test_generic_provisioner_deprovision_handles_exception(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents.generic_provisioner import (
        GenericProvisionerTool,
    )

    tool = GenericProvisionerTool(tool_name="t")
    tool._state = ProvisionerStateStore("generic_t_provisioner", storage_dir=tmp_path)
    tool._state.put("a1", {"permissions": ["read"]})

    with patch.object(tool._state, "delete", side_effect=RuntimeError("io")):
        out = tool.deprovision("a1")
    assert out.success is False
    assert "io" in out.error


def test_create_custom_provisioner_attaches_callbacks(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents.generic_provisioner import (
        create_custom_provisioner,
    )

    calls = {"prov": 0, "verify": 0, "deprov": 0}

    def my_provision(self, agent_id, config, credentials):
        calls["prov"] += 1
        return self._make_success_result(credentials, ["read"], {"ok": True})

    def my_verify(self, agent_id):
        calls["verify"] += 1
        return self._make_verification(passed=True, actual_permissions=[])

    def my_deprovision(self, agent_id):
        from agent_team_studio.agent_provisioning_team.models import DeprovisionResult

        calls["deprov"] += 1
        return DeprovisionResult(tool_name="x", success=True)

    prov = create_custom_provisioner(
        "mytool", provision_fn=my_provision, verify_fn=my_verify, deprovision_fn=my_deprovision
    )
    prov._state = ProvisionerStateStore("generic_mytool_provisioner", storage_dir=tmp_path)

    prov.provision("a", {}, GeneratedCredentials(tool_name="mytool"))
    prov.verify_access("a")
    prov.deprovision("a")
    assert calls == {"prov": 1, "verify": 1, "deprov": 1}


def test_create_custom_provisioner_no_overrides_works(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents.generic_provisioner import (
        create_custom_provisioner,
    )

    prov = create_custom_provisioner("plain")
    prov._state = ProvisionerStateStore("generic_plain_provisioner", storage_dir=tmp_path)
    out = prov.provision("a", {"permissions": ["x"]}, GeneratedCredentials(tool_name="plain"))
    assert out.success


# ---------------------------------------------------------------------------
# postgres_provisioner.py
# ---------------------------------------------------------------------------


def test_postgres_provision_returns_error_when_psycopg_missing(tmp_path: Path, monkeypatch) -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents import postgres_provisioner as pgm
    from agent_team_studio.agent_provisioning_team.tool_agents.postgres_provisioner import (
        PostgresProvisionerTool,
    )

    monkeypatch.setattr(pgm, "HAS_PSYCOPG", False)
    prov = PostgresProvisionerTool()
    prov._state = ProvisionerStateStore("postgres_provisioner", storage_dir=tmp_path)

    out = prov.provision("a1", {}, GeneratedCredentials(tool_name="pg", username="u", password="p"))
    assert out.success is False
    assert "psycopg is not installed" in out.error


def test_postgres_deprovision_no_psycopg(monkeypatch, tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents import postgres_provisioner as pgm
    from agent_team_studio.agent_provisioning_team.tool_agents.postgres_provisioner import (
        PostgresProvisionerTool,
    )

    monkeypatch.setattr(pgm, "HAS_PSYCOPG", False)
    prov = PostgresProvisionerTool()
    prov._state = ProvisionerStateStore("postgres_provisioner", storage_dir=tmp_path)

    out = prov.deprovision("a1")
    assert out.success is False
    assert "psycopg" in out.error


def test_postgres_deprovision_no_state(tmp_path: Path, monkeypatch) -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents import postgres_provisioner as pgm
    from agent_team_studio.agent_provisioning_team.tool_agents.postgres_provisioner import (
        PostgresProvisionerTool,
    )

    monkeypatch.setattr(pgm, "HAS_PSYCOPG", True)
    prov = PostgresProvisionerTool()
    prov._state = ProvisionerStateStore("postgres_provisioner", storage_dir=tmp_path)

    out = prov.deprovision("missing")
    assert out.success is True
    assert "No database" in out.details["message"]


def test_postgres_verify_access_no_state(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents.postgres_provisioner import (
        PostgresProvisionerTool,
    )

    prov = PostgresProvisionerTool()
    prov._state = ProvisionerStateStore("postgres_provisioner", storage_dir=tmp_path)
    v = prov.verify_access("missing")
    assert v.passed is False
    assert "No PostgreSQL" in v.errors[0]


def test_postgres_verify_access_with_state(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents.postgres_provisioner import (
        PostgresProvisionerTool,
    )

    prov = PostgresProvisionerTool()
    prov._state = ProvisionerStateStore("postgres_provisioner", storage_dir=tmp_path)
    prov._state.put("a1", {"permissions": ["ALL PRIVILEGES"]})

    v = prov.verify_access("a1")
    assert v.passed is True
    assert v.actual_permissions == ["ALL PRIVILEGES"]


def test_postgres_do_provision_no_password_raises(tmp_path: Path, monkeypatch) -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents import postgres_provisioner as pgm
    from agent_team_studio.agent_provisioning_team.tool_agents.postgres_provisioner import (
        PostgresProvisionerTool,
    )

    monkeypatch.setattr(pgm, "HAS_PSYCOPG", True)
    prov = PostgresProvisionerTool()
    prov._state = ProvisionerStateStore("postgres_provisioner", storage_dir=tmp_path)

    # Mock the connection layer so we never need a real DB
    fake_conn = MagicMock()
    fake_cursor = MagicMock()
    fake_conn.cursor.return_value = fake_cursor
    with patch.object(prov, "_get_admin_connection", return_value=fake_conn):
        result = prov.provision(
            "a1", {}, GeneratedCredentials(tool_name="pg", username="u", password=None)
        )

    assert result.success is False
    assert "No password" in result.error


def test_postgres_provision_full_path(tmp_path: Path, monkeypatch) -> None:
    """Cover the full _do_provision path: create role + create db + apply perms."""

    from agent_team_studio.agent_provisioning_team.tool_agents import postgres_provisioner as pgm
    from agent_team_studio.agent_provisioning_team.tool_agents.postgres_provisioner import (
        PostgresProvisionerTool,
    )

    monkeypatch.setattr(pgm, "HAS_PSYCOPG", True)
    prov = PostgresProvisionerTool(host="h", port=5432, admin_user="u", admin_password="p")
    prov._state = ProvisionerStateStore("postgres_provisioner", storage_dir=tmp_path)

    fake_conn = MagicMock()
    fake_cursor = MagicMock()
    fake_conn.cursor.return_value = fake_cursor

    # First and second execute calls succeed (CREATE USER + CREATE DATABASE)
    fake_cursor.execute.return_value = None

    with patch.object(prov, "_get_admin_connection", return_value=fake_conn):
        result = prov.provision(
            "agent-1",
            {"database_prefix": "x_"},
            GeneratedCredentials(tool_name="pg", username="u1", password="pw"),
        )

    assert result.success is True
    assert "ALL PRIVILEGES" in result.permissions
    assert result.details["database"].startswith("x_")


def test_postgres_provision_handles_duplicate_role(tmp_path: Path, monkeypatch) -> None:
    # psycopg is a required dependency (agent_provisioning_team/requirements.txt),
    # not optional, so this imports directly rather than pytest.importorskip-guarding.
    import psycopg

    from agent_team_studio.agent_provisioning_team.tool_agents import postgres_provisioner as pgm
    from agent_team_studio.agent_provisioning_team.tool_agents.postgres_provisioner import (
        PostgresProvisionerTool,
    )

    monkeypatch.setattr(pgm, "HAS_PSYCOPG", True)
    prov = PostgresProvisionerTool()
    prov._state = ProvisionerStateStore("postgres_provisioner", storage_dir=tmp_path)

    fake_conn = MagicMock()
    fake_cursor = MagicMock()
    fake_conn.cursor.return_value = fake_cursor

    calls = {"n": 0}

    def execute(*a, **kw):
        calls["n"] += 1
        # First call: CREATE USER → raise DuplicateObject; second call: ALTER USER → ok
        if calls["n"] == 1:
            raise psycopg.errors.DuplicateObject("already exists")
        # Third call: CREATE DATABASE → raise DuplicateDatabase
        if calls["n"] == 3:
            raise psycopg.errors.DuplicateDatabase("already exists")
        return None

    fake_cursor.execute.side_effect = execute

    with patch.object(prov, "_get_admin_connection", return_value=fake_conn):
        result = prov.provision(
            "agent-1",
            {},
            GeneratedCredentials(tool_name="pg", username="u", password="pw"),
        )

    assert result.success is True


def test_postgres_deprovision_full_path(tmp_path: Path, monkeypatch) -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents import postgres_provisioner as pgm
    from agent_team_studio.agent_provisioning_team.tool_agents.postgres_provisioner import (
        PostgresProvisionerTool,
    )

    monkeypatch.setattr(pgm, "HAS_PSYCOPG", True)
    prov = PostgresProvisionerTool()
    prov._state = ProvisionerStateStore("postgres_provisioner", storage_dir=tmp_path)
    prov._state.put("a1", {"database": "agent_a1", "username": "u1"})

    fake_conn = MagicMock()
    fake_cursor = MagicMock()
    fake_conn.cursor.return_value = fake_cursor

    with patch.object(prov, "_get_admin_connection", return_value=fake_conn):
        out = prov.deprovision("a1")

    assert out.success is True
    assert out.details["database_dropped"] == "agent_a1"
    assert out.details["user_dropped"] == "u1"


def test_postgres_deprovision_handles_exception(tmp_path: Path, monkeypatch) -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents import postgres_provisioner as pgm
    from agent_team_studio.agent_provisioning_team.tool_agents.postgres_provisioner import (
        PostgresProvisionerTool,
    )

    monkeypatch.setattr(pgm, "HAS_PSYCOPG", True)
    prov = PostgresProvisionerTool()
    prov._state = ProvisionerStateStore("postgres_provisioner", storage_dir=tmp_path)
    prov._state.put("a1", {"database": "agent_a1", "username": "u1"})

    with patch.object(prov, "_get_admin_connection", side_effect=RuntimeError("down")):
        out = prov.deprovision("a1")
    assert out.success is False
    assert "down" in out.error


def test_postgres_replay_compensation_database(tmp_path: Path, monkeypatch) -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents import postgres_provisioner as pgm
    from agent_team_studio.agent_provisioning_team.tool_agents.postgres_provisioner import (
        PostgresProvisionerTool,
    )

    monkeypatch.setattr(pgm, "HAS_PSYCOPG", True)
    prov = PostgresProvisionerTool()
    prov._state = ProvisionerStateStore("postgres_provisioner", storage_dir=tmp_path)

    fake_conn = MagicMock()
    fake_cursor = MagicMock()
    fake_conn.cursor.return_value = fake_cursor

    with patch.object(prov, "_get_admin_connection", return_value=fake_conn):
        prov.replay_compensation("a1", "postgres.drop_database", {"database": "d1"})

    # Should have called execute at least once
    assert fake_cursor.execute.call_count >= 1


def test_postgres_replay_compensation_role(tmp_path: Path, monkeypatch) -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents import postgres_provisioner as pgm
    from agent_team_studio.agent_provisioning_team.tool_agents.postgres_provisioner import (
        PostgresProvisionerTool,
    )

    monkeypatch.setattr(pgm, "HAS_PSYCOPG", True)
    prov = PostgresProvisionerTool()
    prov._state = ProvisionerStateStore("postgres_provisioner", storage_dir=tmp_path)

    fake_conn = MagicMock()
    fake_cursor = MagicMock()
    fake_conn.cursor.return_value = fake_cursor

    with patch.object(prov, "_get_admin_connection", return_value=fake_conn):
        prov.replay_compensation("a1", "postgres.drop_role", {"username": "u1"})

    assert fake_cursor.execute.call_count == 1


def test_postgres_replay_compensation_unknown_falls_through(tmp_path: Path, monkeypatch) -> None:
    """Unknown kind falls through to base class which just logs and returns."""
    from agent_team_studio.agent_provisioning_team.tool_agents import postgres_provisioner as pgm
    from agent_team_studio.agent_provisioning_team.tool_agents.postgres_provisioner import (
        PostgresProvisionerTool,
    )

    monkeypatch.setattr(pgm, "HAS_PSYCOPG", True)
    prov = PostgresProvisionerTool()
    prov._state = ProvisionerStateStore("postgres_provisioner", storage_dir=tmp_path)

    fake_conn = MagicMock()
    fake_cursor = MagicMock()
    fake_conn.cursor.return_value = fake_cursor

    with patch.object(prov, "_get_admin_connection", return_value=fake_conn):
        prov.replay_compensation("a1", "unknown.kind", {"k": "v"})


def test_postgres_replay_compensation_raises_no_psycopg(tmp_path: Path, monkeypatch) -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents import postgres_provisioner as pgm
    from agent_team_studio.agent_provisioning_team.tool_agents.postgres_provisioner import (
        PostgresProvisionerTool,
    )

    monkeypatch.setattr(pgm, "HAS_PSYCOPG", False)
    prov = PostgresProvisionerTool()
    prov._state = ProvisionerStateStore("postgres_provisioner", storage_dir=tmp_path)

    with pytest.raises(RuntimeError, match="psycopg"):
        prov.replay_compensation("a1", "postgres.drop_role", {"username": "u"})


def test_postgres_apply_permissions_specific(tmp_path: Path, monkeypatch) -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents import postgres_provisioner as pgm
    from agent_team_studio.agent_provisioning_team.tool_agents.postgres_provisioner import (
        PostgresProvisionerTool,
    )

    monkeypatch.setattr(pgm, "HAS_PSYCOPG", True)
    prov = PostgresProvisionerTool()

    fake_cursor = MagicMock()
    prov._apply_permissions(fake_cursor, "db1", "u1", ["SELECT", "INSERT", "CREATE"])

    # Should have called execute for each granted permission
    assert fake_cursor.execute.call_count == 3


# ---------------------------------------------------------------------------
# redis_provisioner.py
# ---------------------------------------------------------------------------


def test_redis_provision_no_lib_returns_error(tmp_path: Path, monkeypatch) -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents import redis_provisioner as rm
    from agent_team_studio.agent_provisioning_team.tool_agents.redis_provisioner import (
        RedisProvisionerTool,
    )

    monkeypatch.setattr(rm, "HAS_REDIS", False)
    prov = RedisProvisionerTool()
    prov._state = ProvisionerStateStore("redis_provisioner", storage_dir=tmp_path)

    out = prov.provision(
        "a1", {}, GeneratedCredentials(tool_name="redis", username="u", password="p")
    )
    assert out.success is False
    assert "redis package" in out.error


def test_redis_deprovision_no_lib(tmp_path: Path, monkeypatch) -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents import redis_provisioner as rm
    from agent_team_studio.agent_provisioning_team.tool_agents.redis_provisioner import (
        RedisProvisionerTool,
    )

    monkeypatch.setattr(rm, "HAS_REDIS", False)
    prov = RedisProvisionerTool()
    prov._state = ProvisionerStateStore("redis_provisioner", storage_dir=tmp_path)

    out = prov.deprovision("a1")
    assert out.success is False


def test_redis_deprovision_no_state(tmp_path: Path, monkeypatch) -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents import redis_provisioner as rm
    from agent_team_studio.agent_provisioning_team.tool_agents.redis_provisioner import (
        RedisProvisionerTool,
    )

    monkeypatch.setattr(rm, "HAS_REDIS", True)
    prov = RedisProvisionerTool()
    prov._state = ProvisionerStateStore("redis_provisioner", storage_dir=tmp_path)

    out = prov.deprovision("missing")
    assert out.success is True
    assert "No Redis ACL" in out.details["message"]


def test_redis_verify_access_no_state(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents.redis_provisioner import (
        RedisProvisionerTool,
    )

    prov = RedisProvisionerTool()
    prov._state = ProvisionerStateStore("redis_provisioner", storage_dir=tmp_path)

    v = prov.verify_access("missing")
    assert v.passed is False
    assert "No Redis" in v.errors[0]


def test_redis_verify_access_with_state(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents.redis_provisioner import (
        RedisProvisionerTool,
    )

    prov = RedisProvisionerTool()
    prov._state = ProvisionerStateStore("redis_provisioner", storage_dir=tmp_path)
    prov._state.put("a1", {"permissions": ["+@all"]})

    v = prov.verify_access("a1")
    assert v.passed is True


def test_redis_build_acl_rules_full_access() -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents.redis_provisioner import (
        RedisProvisionerTool,
    )

    prov = RedisProvisionerTool()
    assert prov._build_acl_rules(["+@all"], "k:") == ["+@all"]


def test_redis_build_acl_rules_subset() -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents.redis_provisioner import (
        RedisProvisionerTool,
    )

    prov = RedisProvisionerTool()
    rules = prov._build_acl_rules(["GET", "SET", "DEL"], "k:")
    assert "-@all" in rules
    assert "+get" in rules
    assert "+set" in rules
    assert "+del" in rules
    # Always-on commands
    assert "+ping" in rules
    assert "+auth" in rules


def test_redis_do_provision_no_password_raises(tmp_path: Path, monkeypatch) -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents import redis_provisioner as rm
    from agent_team_studio.agent_provisioning_team.tool_agents.redis_provisioner import (
        RedisProvisionerTool,
    )

    monkeypatch.setattr(rm, "HAS_REDIS", True)
    prov = RedisProvisionerTool()
    prov._state = ProvisionerStateStore("redis_provisioner", storage_dir=tmp_path)

    fake_client = MagicMock()
    with patch.object(prov, "_get_admin_client", return_value=fake_client):
        out = prov.provision(
            "a1", {}, GeneratedCredentials(tool_name="redis", username="u", password=None)
        )

    assert out.success is False
    assert "No password" in out.error


def test_redis_provision_full_path(tmp_path: Path, monkeypatch) -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents import redis_provisioner as rm
    from agent_team_studio.agent_provisioning_team.tool_agents.redis_provisioner import (
        RedisProvisionerTool,
    )

    monkeypatch.setattr(rm, "HAS_REDIS", True)
    prov = RedisProvisionerTool(host="h", port=6379, admin_password="adminpw")
    prov._state = ProvisionerStateStore("redis_provisioner", storage_dir=tmp_path)

    fake_client = MagicMock()
    with patch.object(prov, "_get_admin_client", return_value=fake_client):
        result = prov.provision(
            "agent-1",
            {"key_prefix": "ag:"},
            GeneratedCredentials(tool_name="redis", username="u1", password="pw"),
        )

    assert result.success is True
    fake_client.acl_setuser.assert_called_once()


def test_redis_provision_handles_deluser_response_error(tmp_path: Path, monkeypatch) -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents import redis_provisioner as rm
    from agent_team_studio.agent_provisioning_team.tool_agents.redis_provisioner import (
        RedisProvisionerTool,
    )

    # Avoid a hard dependency on the optional ``redis`` package: synthesize a
    # local stub exposing ``exceptions.ResponseError`` and patch the module
    # attribute so the production ``except redis.exceptions.ResponseError``
    # clause catches our local exception type.
    class _StubResponseError(Exception):
        pass

    class _StubExceptions:
        ResponseError = _StubResponseError

    class _StubRedis:
        exceptions = _StubExceptions

    monkeypatch.setattr(rm, "redis", _StubRedis, raising=False)
    monkeypatch.setattr(rm, "HAS_REDIS", True)
    prov = RedisProvisionerTool()
    prov._state = ProvisionerStateStore("redis_provisioner", storage_dir=tmp_path)

    fake_client = MagicMock()
    fake_client.acl_deluser.side_effect = _StubResponseError("nope")
    with patch.object(prov, "_get_admin_client", return_value=fake_client):
        result = prov.provision(
            "a1",
            {},
            GeneratedCredentials(tool_name="redis", username="u", password="pw"),
        )

    # deluser raising ResponseError must be swallowed
    assert result.success is True


def test_redis_deprovision_full_path(tmp_path: Path, monkeypatch) -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents import redis_provisioner as rm
    from agent_team_studio.agent_provisioning_team.tool_agents.redis_provisioner import (
        RedisProvisionerTool,
    )

    monkeypatch.setattr(rm, "HAS_REDIS", True)
    prov = RedisProvisionerTool()
    prov._state = ProvisionerStateStore("redis_provisioner", storage_dir=tmp_path)
    prov._state.put("a1", {"username": "u1"})

    fake_client = MagicMock()
    with patch.object(prov, "_get_admin_client", return_value=fake_client):
        out = prov.deprovision("a1")

    assert out.success is True
    assert out.details["user_deleted"] == "u1"


def test_redis_deprovision_handles_response_error(tmp_path: Path, monkeypatch) -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents import redis_provisioner as rm
    from agent_team_studio.agent_provisioning_team.tool_agents.redis_provisioner import (
        RedisProvisionerTool,
    )

    # Avoid a hard dependency on the optional ``redis`` package: synthesize a
    # local stub exposing ``exceptions.ResponseError`` and patch the module
    # attribute so the production ``except redis.exceptions.ResponseError``
    # clause catches our local exception type.
    class _StubResponseError(Exception):
        pass

    class _StubExceptions:
        ResponseError = _StubResponseError

    class _StubRedis:
        exceptions = _StubExceptions

    monkeypatch.setattr(rm, "redis", _StubRedis, raising=False)
    monkeypatch.setattr(rm, "HAS_REDIS", True)
    prov = RedisProvisionerTool()
    prov._state = ProvisionerStateStore("redis_provisioner", storage_dir=tmp_path)
    prov._state.put("a1", {"username": "u1"})

    fake_client = MagicMock()
    fake_client.acl_deluser.side_effect = _StubResponseError("missing user")
    with patch.object(prov, "_get_admin_client", return_value=fake_client):
        out = prov.deprovision("a1")

    assert out.success is True


def test_redis_deprovision_handles_exception(tmp_path: Path, monkeypatch) -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents import redis_provisioner as rm
    from agent_team_studio.agent_provisioning_team.tool_agents.redis_provisioner import (
        RedisProvisionerTool,
    )

    monkeypatch.setattr(rm, "HAS_REDIS", True)
    prov = RedisProvisionerTool()
    prov._state = ProvisionerStateStore("redis_provisioner", storage_dir=tmp_path)
    prov._state.put("a1", {"username": "u1"})

    with patch.object(prov, "_get_admin_client", side_effect=RuntimeError("down")):
        out = prov.deprovision("a1")
    assert out.success is False
    assert "down" in out.error


def test_redis_get_admin_client_no_lib(monkeypatch) -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents import redis_provisioner as rm
    from agent_team_studio.agent_provisioning_team.tool_agents.redis_provisioner import (
        RedisProvisionerTool,
    )

    monkeypatch.setattr(rm, "HAS_REDIS", False)
    prov = RedisProvisionerTool()
    with pytest.raises(RuntimeError, match="redis package"):
        prov._get_admin_client()


def test_redis_on_reuse_populates_credentials(tmp_path: Path, monkeypatch) -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents import redis_provisioner as rm
    from agent_team_studio.agent_provisioning_team.tool_agents.redis_provisioner import (
        RedisProvisionerTool,
    )

    monkeypatch.setattr(rm, "HAS_REDIS", True)
    prov = RedisProvisionerTool()
    prov._state = ProvisionerStateStore("redis_provisioner", storage_dir=tmp_path)
    prov._state.put("a1", {"key_prefix": "k:", "permissions": ["+@all"]})

    with patch.object(prov, "_get_admin_client"):
        # Force reuse path by ensuring state already exists
        result = prov.provision(
            "a1", {}, GeneratedCredentials(tool_name="redis", username="u", password="p")
        )
    assert result.success is True
    assert result.details.get("reused") is True


# ---------------------------------------------------------------------------
# postgres_provisioner __init__ env defaults
# ---------------------------------------------------------------------------


def test_postgres_init_uses_env_defaults(monkeypatch) -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents.postgres_provisioner import (
        PostgresProvisionerTool,
    )

    monkeypatch.setenv("POSTGRES_HOST", "envhost")
    monkeypatch.setenv("POSTGRES_PORT", "9999")
    monkeypatch.setenv("POSTGRES_USER", "envuser")
    monkeypatch.setenv("POSTGRES_PASSWORD", "fixture-placeholder-not-a-secret")

    prov = PostgresProvisionerTool()
    assert prov.host == "envhost"
    assert prov.port == 9999
    assert prov.admin_user == "envuser"
    assert prov.admin_password == "fixture-placeholder-not-a-secret"


def test_redis_init_uses_env_defaults(monkeypatch) -> None:
    from agent_team_studio.agent_provisioning_team.tool_agents.redis_provisioner import (
        RedisProvisionerTool,
    )

    monkeypatch.setenv("REDIS_HOST", "rhost")
    monkeypatch.setenv("REDIS_PORT", "12345")
    monkeypatch.setenv("REDIS_PASSWORD", "rp")

    prov = RedisProvisionerTool()
    assert prov.host == "rhost"
    assert prov.port == 12345
    assert prov.admin_password == "rp"


# -------------------------------------------------------------------------
# Canonical anatomy preamble exposed on a provisioner instance.
# -------------------------------------------------------------------------


def test_canonical_anatomy_prompt_preamble_via_instance(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.shared.provisioner_state import (
        ProvisionerStateStore,
    )
    from agent_team_studio.agent_provisioning_team.tool_agents.generic_provisioner import (
        GenericProvisionerTool,
    )

    prov = GenericProvisionerTool("x")
    prov._state = ProvisionerStateStore("generic_x_provisioner", storage_dir=tmp_path)
    text = prov.canonical_anatomy_prompt_preamble()
    assert isinstance(text, str)
    assert text.strip()
    assert "anatomy" in text.lower() or "Khala" in text
    assert "Input" in text or "Output" in text or "Tools" in text
