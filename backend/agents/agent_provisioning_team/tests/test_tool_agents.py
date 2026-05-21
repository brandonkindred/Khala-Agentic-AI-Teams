"""Unit tests for tool provisioner agents.

Docker / Postgres / Redis / Git are all mocked at the
subprocess / library boundary — no real services are touched.
"""

from __future__ import annotations

import subprocess as _subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent_provisioning_team.models import GeneratedCredentials
from agent_provisioning_team.shared.provisioner_state import (
    CompensationRecord,
    ProvisionerStateStore,
)
from agent_provisioning_team.tool_agents.base import BaseToolProvisioner

# ---------------------------------------------------------------------------
# base.py
# ---------------------------------------------------------------------------


class _MinimalProv(BaseToolProvisioner):
    tool_name = "minimal"

    def __init__(self, storage_dir: Path) -> None:
        self._state = ProvisionerStateStore("minimal_prov", storage_dir=storage_dir)

    def provision(self, agent_id, config, credentials):
        return self.run_idempotent(
            agent_id,
            credentials=credentials,
            create=lambda _r: (["read"], {"x": 1, "permissions": ["read"]}),
        )

    def verify_access(self, agent_id):
        return self._make_verification(passed=True, actual_permissions=[])

    def deprovision(self, agent_id):
        from agent_provisioning_team.models import DeprovisionResult

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
            from agent_provisioning_team.models import DeprovisionResult

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
            from agent_provisioning_team.models import DeprovisionResult

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
            from agent_provisioning_team.models import DeprovisionResult

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


# ---------------------------------------------------------------------------
# docker_provisioner.py
# ---------------------------------------------------------------------------


def _docker_run_success(*args, **kwargs):
    return SimpleNamespace(returncode=0, stdout="abc123def456789012\n", stderr="")


def test_docker_provisioner_provision_success(tmp_path: Path, monkeypatch) -> None:
    from agent_provisioning_team.tool_agents.docker_provisioner import DockerProvisionerTool

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
    from agent_provisioning_team.tool_agents.docker_provisioner import DockerProvisionerTool

    prov = DockerProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("docker_provisioner", storage_dir=tmp_path)

    def _fail(*a, **kw):
        return SimpleNamespace(returncode=1, stdout="", stderr="something bad")

    with patch("subprocess.run", side_effect=_fail):
        result = prov.provision("agent-1", {}, GeneratedCredentials(tool_name="docker"))

    assert result.success is False
    assert "Docker run failed" in result.error or "something bad" in result.error


def test_docker_provisioner_is_idempotent_on_existing_state(tmp_path: Path) -> None:
    from agent_provisioning_team.tool_agents.docker_provisioner import DockerProvisionerTool

    prov = DockerProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("docker_provisioner", storage_dir=tmp_path)

    with patch("subprocess.run", side_effect=_docker_run_success):
        first = prov.provision("a1", {}, GeneratedCredentials(tool_name="docker"))
        assert first.success

    # Second call doesn't shell out (no patch needed); reuses state
    second = prov.provision("a1", {}, GeneratedCredentials(tool_name="docker"))
    assert second.success
    assert second.details.get("reused") is True


def test_docker_verify_access_no_state(tmp_path: Path) -> None:
    from agent_provisioning_team.tool_agents.docker_provisioner import DockerProvisionerTool

    prov = DockerProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("docker_provisioner", storage_dir=tmp_path)

    v = prov.verify_access("missing-agent")
    assert v.passed is False
    assert "No container" in v.errors[0]


def test_docker_verify_access_success(tmp_path: Path) -> None:
    from agent_provisioning_team.tool_agents.docker_provisioner import DockerProvisionerTool

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
    from agent_provisioning_team.tool_agents.docker_provisioner import DockerProvisionerTool

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
    from agent_provisioning_team.tool_agents.docker_provisioner import DockerProvisionerTool

    prov = DockerProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("docker_provisioner", storage_dir=tmp_path)
    prov._state.put("a1", {"container_name": "c1"})

    with patch("subprocess.run", side_effect=OSError("kaboom")):
        v = prov.verify_access("a1")

    assert v.passed is False
    assert "kaboom" in v.errors[0]


def test_docker_deprovision_no_state(tmp_path: Path) -> None:
    from agent_provisioning_team.tool_agents.docker_provisioner import DockerProvisionerTool

    prov = DockerProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("docker_provisioner", storage_dir=tmp_path)

    out = prov.deprovision("nobody")
    assert out.success is True
    assert "No container" in out.details["message"]


def test_docker_deprovision_with_state(tmp_path: Path) -> None:
    from agent_provisioning_team.tool_agents.docker_provisioner import DockerProvisionerTool

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


def test_docker_deprovision_handles_exception(tmp_path: Path) -> None:
    from agent_provisioning_team.tool_agents.docker_provisioner import DockerProvisionerTool

    prov = DockerProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("docker_provisioner", storage_dir=tmp_path)
    prov._state.put("a1", {"container_name": "c1"})

    with patch("subprocess.run", side_effect=RuntimeError("daemon down")):
        out = prov.deprovision("a1")

    assert out.success is False
    assert "daemon down" in out.error


def test_docker_allocate_port_is_deterministic() -> None:
    from agent_provisioning_team.tool_agents.docker_provisioner import DockerProvisionerTool

    prov = DockerProvisionerTool()
    p1 = prov._allocate_port("agent-a")
    p2 = prov._allocate_port("agent-a")
    assert p1 == p2
    assert 22000 <= p1 < 23000


def test_docker_get_container_info(tmp_path: Path) -> None:
    from agent_provisioning_team.tool_agents.docker_provisioner import DockerProvisionerTool

    prov = DockerProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("docker_provisioner", storage_dir=tmp_path)
    prov._state.put("a1", {"container_name": "c1"})

    info = prov.get_container_info("a1")
    assert info["container_name"] == "c1"
    assert prov.get_container_info("missing") is None


def test_docker_exec_in_container_no_state(tmp_path: Path) -> None:
    from agent_provisioning_team.tool_agents.docker_provisioner import DockerProvisionerTool

    prov = DockerProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("docker_provisioner", storage_dir=tmp_path)

    rc, out, err = prov.exec_in_container("missing", ["ls"])
    assert rc == 1
    assert "No container" in err


def test_docker_exec_in_container_runs(tmp_path: Path) -> None:
    from agent_provisioning_team.tool_agents.docker_provisioner import DockerProvisionerTool

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
    from agent_provisioning_team.tool_agents.docker_provisioner import DockerProvisionerTool

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
    from agent_provisioning_team.tool_agents.docker_provisioner import DockerProvisionerTool

    prov = DockerProvisionerTool(workspace_base=str(tmp_path))
    prov._state = ProvisionerStateStore("docker_provisioner", storage_dir=tmp_path)
    prov._state.put("a1", {"container_name": "c1"})

    with patch("subprocess.run", side_effect=OSError("boom")):
        rc, out, err = prov.exec_in_container("a1", ["x"])
    assert rc == 1
    assert "boom" in err


def test_docker_on_reuse_populates_credentials(tmp_path: Path) -> None:
    """Cover the _on_reuse path: stored state + fresh creds."""
    from agent_provisioning_team.tool_agents.docker_provisioner import DockerProvisionerTool

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
    result = prov.provision("a1", {}, creds)
    assert result.success is True
    assert creds.extra["container_id"] == "abcd"


# ---------------------------------------------------------------------------
# generic_provisioner.py
# ---------------------------------------------------------------------------


def test_generic_provisioner_provision_success(tmp_path: Path) -> None:
    from agent_provisioning_team.tool_agents.generic_provisioner import GenericProvisionerTool

    tool = GenericProvisionerTool(tool_name="mytool")
    tool._state = ProvisionerStateStore("generic_mytool_provisioner", storage_dir=tmp_path)

    creds = GeneratedCredentials(tool_name="mytool")
    out = tool.provision("a1", {"permissions": ["read", "write"]}, creds)
    assert out.success is True
    assert "read" in out.permissions
    assert creds.extra["tool_name"] == "mytool"


def test_generic_provisioner_default_permissions(tmp_path: Path) -> None:
    from agent_provisioning_team.tool_agents.generic_provisioner import GenericProvisionerTool

    tool = GenericProvisionerTool(tool_name="t")
    tool._state = ProvisionerStateStore("generic_t_provisioner", storage_dir=tmp_path)

    out = tool.provision("a1", {}, GeneratedCredentials(tool_name="t"))
    assert out.permissions == ["all"]


def test_generic_provisioner_verify_no_state(tmp_path: Path) -> None:
    from agent_provisioning_team.tool_agents.generic_provisioner import GenericProvisionerTool

    tool = GenericProvisionerTool(tool_name="t")
    tool._state = ProvisionerStateStore("generic_t_provisioner", storage_dir=tmp_path)

    v = tool.verify_access("missing")
    assert v.passed is False
    assert "No provisioning" in v.errors[0]


def test_generic_provisioner_verify_with_state(tmp_path: Path) -> None:
    from agent_provisioning_team.tool_agents.generic_provisioner import GenericProvisionerTool

    tool = GenericProvisionerTool(tool_name="t")
    tool._state = ProvisionerStateStore("generic_t_provisioner", storage_dir=tmp_path)
    tool._state.put("a1", {"permissions": ["read"]})

    v = tool.verify_access("a1")
    assert v.passed is True
    assert v.actual_permissions == ["read"]


def test_generic_provisioner_deprovision_no_state(tmp_path: Path) -> None:
    from agent_provisioning_team.tool_agents.generic_provisioner import GenericProvisionerTool

    tool = GenericProvisionerTool(tool_name="t")
    tool._state = ProvisionerStateStore("generic_t_provisioner", storage_dir=tmp_path)

    out = tool.deprovision("missing")
    assert out.success is True
    assert "No provisioning" in out.details["message"]


def test_generic_provisioner_deprovision_with_state(tmp_path: Path) -> None:
    from agent_provisioning_team.tool_agents.generic_provisioner import GenericProvisionerTool

    tool = GenericProvisionerTool(tool_name="t")
    tool._state = ProvisionerStateStore("generic_t_provisioner", storage_dir=tmp_path)
    tool._state.put("a1", {"permissions": ["read"]})

    out = tool.deprovision("a1")
    assert out.success is True
    assert out.details["deprovisioned"] is True


def test_generic_provisioner_deprovision_handles_exception(tmp_path: Path) -> None:
    from agent_provisioning_team.tool_agents.generic_provisioner import GenericProvisionerTool

    tool = GenericProvisionerTool(tool_name="t")
    tool._state = ProvisionerStateStore("generic_t_provisioner", storage_dir=tmp_path)
    tool._state.put("a1", {"permissions": ["read"]})

    with patch.object(tool._state, "delete", side_effect=RuntimeError("io")):
        out = tool.deprovision("a1")
    assert out.success is False
    assert "io" in out.error


def test_create_custom_provisioner_attaches_callbacks(tmp_path: Path) -> None:
    from agent_provisioning_team.tool_agents.generic_provisioner import (
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
        from agent_provisioning_team.models import DeprovisionResult

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
    from agent_provisioning_team.tool_agents.generic_provisioner import (
        create_custom_provisioner,
    )

    prov = create_custom_provisioner("plain")
    prov._state = ProvisionerStateStore("generic_plain_provisioner", storage_dir=tmp_path)
    out = prov.provision("a", {"permissions": ["x"]}, GeneratedCredentials(tool_name="plain"))
    assert out.success


# ---------------------------------------------------------------------------
# postgres_provisioner.py
# ---------------------------------------------------------------------------


def test_postgres_provision_returns_error_when_psycopg2_missing(
    tmp_path: Path, monkeypatch
) -> None:
    from agent_provisioning_team.tool_agents import postgres_provisioner as pgm
    from agent_provisioning_team.tool_agents.postgres_provisioner import PostgresProvisionerTool

    monkeypatch.setattr(pgm, "HAS_PSYCOPG2", False)
    prov = PostgresProvisionerTool()
    prov._state = ProvisionerStateStore("postgres_provisioner", storage_dir=tmp_path)

    out = prov.provision("a1", {}, GeneratedCredentials(tool_name="pg", username="u", password="p"))
    assert out.success is False
    assert "psycopg2 is not installed" in out.error


def test_postgres_deprovision_no_psycopg2(monkeypatch, tmp_path: Path) -> None:
    from agent_provisioning_team.tool_agents import postgres_provisioner as pgm
    from agent_provisioning_team.tool_agents.postgres_provisioner import PostgresProvisionerTool

    monkeypatch.setattr(pgm, "HAS_PSYCOPG2", False)
    prov = PostgresProvisionerTool()
    prov._state = ProvisionerStateStore("postgres_provisioner", storage_dir=tmp_path)

    out = prov.deprovision("a1")
    assert out.success is False
    assert "psycopg2" in out.error


def test_postgres_deprovision_no_state(tmp_path: Path, monkeypatch) -> None:
    from agent_provisioning_team.tool_agents import postgres_provisioner as pgm
    from agent_provisioning_team.tool_agents.postgres_provisioner import PostgresProvisionerTool

    monkeypatch.setattr(pgm, "HAS_PSYCOPG2", True)
    prov = PostgresProvisionerTool()
    prov._state = ProvisionerStateStore("postgres_provisioner", storage_dir=tmp_path)

    out = prov.deprovision("missing")
    assert out.success is True
    assert "No database" in out.details["message"]


def test_postgres_verify_access_no_state(tmp_path: Path) -> None:
    from agent_provisioning_team.tool_agents.postgres_provisioner import PostgresProvisionerTool

    prov = PostgresProvisionerTool()
    prov._state = ProvisionerStateStore("postgres_provisioner", storage_dir=tmp_path)
    v = prov.verify_access("missing")
    assert v.passed is False
    assert "No PostgreSQL" in v.errors[0]


def test_postgres_verify_access_with_state(tmp_path: Path) -> None:
    from agent_provisioning_team.tool_agents.postgres_provisioner import PostgresProvisionerTool

    prov = PostgresProvisionerTool()
    prov._state = ProvisionerStateStore("postgres_provisioner", storage_dir=tmp_path)
    prov._state.put("a1", {"permissions": ["ALL PRIVILEGES"]})

    v = prov.verify_access("a1")
    assert v.passed is True
    assert v.actual_permissions == ["ALL PRIVILEGES"]


def test_postgres_do_provision_no_password_raises(tmp_path: Path, monkeypatch) -> None:
    from agent_provisioning_team.tool_agents import postgres_provisioner as pgm
    from agent_provisioning_team.tool_agents.postgres_provisioner import PostgresProvisionerTool

    monkeypatch.setattr(pgm, "HAS_PSYCOPG2", True)
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

    from agent_provisioning_team.tool_agents import postgres_provisioner as pgm
    from agent_provisioning_team.tool_agents.postgres_provisioner import PostgresProvisionerTool

    monkeypatch.setattr(pgm, "HAS_PSYCOPG2", True)
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
    import psycopg2

    from agent_provisioning_team.tool_agents import postgres_provisioner as pgm
    from agent_provisioning_team.tool_agents.postgres_provisioner import PostgresProvisionerTool

    monkeypatch.setattr(pgm, "HAS_PSYCOPG2", True)
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
            raise psycopg2.errors.DuplicateObject("already exists")
        # Third call: CREATE DATABASE → raise DuplicateDatabase
        if calls["n"] == 3:
            raise psycopg2.errors.DuplicateDatabase("already exists")
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
    from agent_provisioning_team.tool_agents import postgres_provisioner as pgm
    from agent_provisioning_team.tool_agents.postgres_provisioner import PostgresProvisionerTool

    monkeypatch.setattr(pgm, "HAS_PSYCOPG2", True)
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
    from agent_provisioning_team.tool_agents import postgres_provisioner as pgm
    from agent_provisioning_team.tool_agents.postgres_provisioner import PostgresProvisionerTool

    monkeypatch.setattr(pgm, "HAS_PSYCOPG2", True)
    prov = PostgresProvisionerTool()
    prov._state = ProvisionerStateStore("postgres_provisioner", storage_dir=tmp_path)
    prov._state.put("a1", {"database": "agent_a1", "username": "u1"})

    with patch.object(prov, "_get_admin_connection", side_effect=RuntimeError("down")):
        out = prov.deprovision("a1")
    assert out.success is False
    assert "down" in out.error


def test_postgres_replay_compensation_database(tmp_path: Path, monkeypatch) -> None:
    from agent_provisioning_team.tool_agents import postgres_provisioner as pgm
    from agent_provisioning_team.tool_agents.postgres_provisioner import PostgresProvisionerTool

    monkeypatch.setattr(pgm, "HAS_PSYCOPG2", True)
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
    from agent_provisioning_team.tool_agents import postgres_provisioner as pgm
    from agent_provisioning_team.tool_agents.postgres_provisioner import PostgresProvisionerTool

    monkeypatch.setattr(pgm, "HAS_PSYCOPG2", True)
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
    from agent_provisioning_team.tool_agents import postgres_provisioner as pgm
    from agent_provisioning_team.tool_agents.postgres_provisioner import PostgresProvisionerTool

    monkeypatch.setattr(pgm, "HAS_PSYCOPG2", True)
    prov = PostgresProvisionerTool()
    prov._state = ProvisionerStateStore("postgres_provisioner", storage_dir=tmp_path)

    fake_conn = MagicMock()
    fake_cursor = MagicMock()
    fake_conn.cursor.return_value = fake_cursor

    with patch.object(prov, "_get_admin_connection", return_value=fake_conn):
        prov.replay_compensation("a1", "unknown.kind", {"k": "v"})


def test_postgres_replay_compensation_raises_no_psycopg2(tmp_path: Path, monkeypatch) -> None:
    from agent_provisioning_team.tool_agents import postgres_provisioner as pgm
    from agent_provisioning_team.tool_agents.postgres_provisioner import PostgresProvisionerTool

    monkeypatch.setattr(pgm, "HAS_PSYCOPG2", False)
    prov = PostgresProvisionerTool()
    prov._state = ProvisionerStateStore("postgres_provisioner", storage_dir=tmp_path)

    with pytest.raises(RuntimeError, match="psycopg2"):
        prov.replay_compensation("a1", "postgres.drop_role", {"username": "u"})


def test_postgres_apply_permissions_specific(tmp_path: Path, monkeypatch) -> None:
    from agent_provisioning_team.tool_agents import postgres_provisioner as pgm
    from agent_provisioning_team.tool_agents.postgres_provisioner import PostgresProvisionerTool

    monkeypatch.setattr(pgm, "HAS_PSYCOPG2", True)
    prov = PostgresProvisionerTool()

    fake_cursor = MagicMock()
    prov._apply_permissions(fake_cursor, "db1", "u1", ["SELECT", "INSERT", "CREATE"])

    # Should have called execute for each granted permission
    assert fake_cursor.execute.call_count == 3


# ---------------------------------------------------------------------------
# redis_provisioner.py
# ---------------------------------------------------------------------------


def test_redis_provision_no_lib_returns_error(tmp_path: Path, monkeypatch) -> None:
    from agent_provisioning_team.tool_agents import redis_provisioner as rm
    from agent_provisioning_team.tool_agents.redis_provisioner import RedisProvisionerTool

    monkeypatch.setattr(rm, "HAS_REDIS", False)
    prov = RedisProvisionerTool()
    prov._state = ProvisionerStateStore("redis_provisioner", storage_dir=tmp_path)

    out = prov.provision(
        "a1", {}, GeneratedCredentials(tool_name="redis", username="u", password="p")
    )
    assert out.success is False
    assert "redis package" in out.error


def test_redis_deprovision_no_lib(tmp_path: Path, monkeypatch) -> None:
    from agent_provisioning_team.tool_agents import redis_provisioner as rm
    from agent_provisioning_team.tool_agents.redis_provisioner import RedisProvisionerTool

    monkeypatch.setattr(rm, "HAS_REDIS", False)
    prov = RedisProvisionerTool()
    prov._state = ProvisionerStateStore("redis_provisioner", storage_dir=tmp_path)

    out = prov.deprovision("a1")
    assert out.success is False


def test_redis_deprovision_no_state(tmp_path: Path, monkeypatch) -> None:
    from agent_provisioning_team.tool_agents import redis_provisioner as rm
    from agent_provisioning_team.tool_agents.redis_provisioner import RedisProvisionerTool

    monkeypatch.setattr(rm, "HAS_REDIS", True)
    prov = RedisProvisionerTool()
    prov._state = ProvisionerStateStore("redis_provisioner", storage_dir=tmp_path)

    out = prov.deprovision("missing")
    assert out.success is True
    assert "No Redis ACL" in out.details["message"]


def test_redis_verify_access_no_state(tmp_path: Path) -> None:
    from agent_provisioning_team.tool_agents.redis_provisioner import RedisProvisionerTool

    prov = RedisProvisionerTool()
    prov._state = ProvisionerStateStore("redis_provisioner", storage_dir=tmp_path)

    v = prov.verify_access("missing")
    assert v.passed is False
    assert "No Redis" in v.errors[0]


def test_redis_verify_access_with_state(tmp_path: Path) -> None:
    from agent_provisioning_team.tool_agents.redis_provisioner import RedisProvisionerTool

    prov = RedisProvisionerTool()
    prov._state = ProvisionerStateStore("redis_provisioner", storage_dir=tmp_path)
    prov._state.put("a1", {"permissions": ["+@all"]})

    v = prov.verify_access("a1")
    assert v.passed is True


def test_redis_build_acl_rules_full_access() -> None:
    from agent_provisioning_team.tool_agents.redis_provisioner import RedisProvisionerTool

    prov = RedisProvisionerTool()
    assert prov._build_acl_rules(["+@all"], "k:") == ["+@all"]


def test_redis_build_acl_rules_subset() -> None:
    from agent_provisioning_team.tool_agents.redis_provisioner import RedisProvisionerTool

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
    from agent_provisioning_team.tool_agents import redis_provisioner as rm
    from agent_provisioning_team.tool_agents.redis_provisioner import RedisProvisionerTool

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
    from agent_provisioning_team.tool_agents import redis_provisioner as rm
    from agent_provisioning_team.tool_agents.redis_provisioner import RedisProvisionerTool

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
    import redis  # type: ignore[import-not-found]

    from agent_provisioning_team.tool_agents import redis_provisioner as rm
    from agent_provisioning_team.tool_agents.redis_provisioner import RedisProvisionerTool

    monkeypatch.setattr(rm, "HAS_REDIS", True)
    prov = RedisProvisionerTool()
    prov._state = ProvisionerStateStore("redis_provisioner", storage_dir=tmp_path)

    fake_client = MagicMock()
    fake_client.acl_deluser.side_effect = redis.exceptions.ResponseError("nope")
    with patch.object(prov, "_get_admin_client", return_value=fake_client):
        result = prov.provision(
            "a1",
            {},
            GeneratedCredentials(tool_name="redis", username="u", password="pw"),
        )

    # deluser raising ResponseError must be swallowed
    assert result.success is True


def test_redis_deprovision_full_path(tmp_path: Path, monkeypatch) -> None:
    from agent_provisioning_team.tool_agents import redis_provisioner as rm
    from agent_provisioning_team.tool_agents.redis_provisioner import RedisProvisionerTool

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
    import redis

    from agent_provisioning_team.tool_agents import redis_provisioner as rm
    from agent_provisioning_team.tool_agents.redis_provisioner import RedisProvisionerTool

    monkeypatch.setattr(rm, "HAS_REDIS", True)
    prov = RedisProvisionerTool()
    prov._state = ProvisionerStateStore("redis_provisioner", storage_dir=tmp_path)
    prov._state.put("a1", {"username": "u1"})

    fake_client = MagicMock()
    fake_client.acl_deluser.side_effect = redis.exceptions.ResponseError("missing user")
    with patch.object(prov, "_get_admin_client", return_value=fake_client):
        out = prov.deprovision("a1")

    assert out.success is True


def test_redis_deprovision_handles_exception(tmp_path: Path, monkeypatch) -> None:
    from agent_provisioning_team.tool_agents import redis_provisioner as rm
    from agent_provisioning_team.tool_agents.redis_provisioner import RedisProvisionerTool

    monkeypatch.setattr(rm, "HAS_REDIS", True)
    prov = RedisProvisionerTool()
    prov._state = ProvisionerStateStore("redis_provisioner", storage_dir=tmp_path)
    prov._state.put("a1", {"username": "u1"})

    with patch.object(prov, "_get_admin_client", side_effect=RuntimeError("down")):
        out = prov.deprovision("a1")
    assert out.success is False
    assert "down" in out.error


def test_redis_get_admin_client_no_lib(monkeypatch) -> None:
    from agent_provisioning_team.tool_agents import redis_provisioner as rm
    from agent_provisioning_team.tool_agents.redis_provisioner import RedisProvisionerTool

    monkeypatch.setattr(rm, "HAS_REDIS", False)
    prov = RedisProvisionerTool()
    with pytest.raises(RuntimeError, match="redis package"):
        prov._get_admin_client()


def test_redis_on_reuse_populates_credentials(tmp_path: Path) -> None:
    from agent_provisioning_team.tool_agents.redis_provisioner import RedisProvisionerTool

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
    from agent_provisioning_team.tool_agents.postgres_provisioner import PostgresProvisionerTool

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
    from agent_provisioning_team.tool_agents.redis_provisioner import RedisProvisionerTool

    monkeypatch.setenv("REDIS_HOST", "rhost")
    monkeypatch.setenv("REDIS_PORT", "12345")
    monkeypatch.setenv("REDIS_PASSWORD", "rp")

    prov = RedisProvisionerTool()
    assert prov.host == "rhost"
    assert prov.port == 12345
    assert prov.admin_password == "rp"
