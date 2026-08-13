"""Tests for the per-sandbox compose-stack provisioner and its Docker helpers.

The provisioner shells out to ``docker compose`` to bring up a self-
contained stack (postgres + temporal + prometheus + grafana + agent) per
sandbox. Tests stub ``_exec`` so no real Docker daemon is required and cover
compose up/down and the rendered compose file, ``_exec`` timeouts,
``is_running`` / ``inspect_host_port``, container naming, and the
secrets/cleanup helpers.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_platform.sandbox import provisioner as provisioner_mod
from agent_platform.sandbox.provisioner import container_name_for


@pytest.fixture
def sandbox_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point AGENT_CACHE at a temp dir so per-sandbox project dirs land there."""
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    return tmp_path


@pytest.mark.asyncio
async def test_run_container_renders_compose_and_mounts_secrets(
    sandbox_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``run_container`` writes a per-project docker-compose.yml + 0400 secrets
    file and invokes ``docker compose up -d``."""
    monkeypatch.setenv("OLLAMA_API_KEY", "ollama-xyz")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    captured: list[list[str]] = []

    async def fake_exec(cmd: list[str], *, timeout_s: int = 30):
        captured.append(cmd)
        if cmd[:2] == ["docker", "compose"]:
            return 0, "", ""
        if cmd[:2] == ["docker", "inspect"]:
            return 0, "abcdef0123456789\n", ""
        return 0, "", ""

    monkeypatch.setattr(provisioner_mod, "_exec", fake_exec)

    container_id = await provisioner_mod.run_container(
        agent_id="blogging.planner",
        container_name="khala-sbx-blog",
        team="blogging",
    )

    assert container_id == "abcdef0123456789"

    # docker compose up was invoked with the right project name + compose file.
    up_argv = next(c for c in captured if c[:2] == ["docker", "compose"] and "up" in c)
    assert up_argv[3] == "khala-sbx-blog"  # -p <project>
    assert up_argv[5].endswith("docker-compose.yml")  # -f <file>
    assert "up" in up_argv and "-d" in up_argv
    assert "--remove-orphans" in up_argv

    # Per-project directory exists and contains the rendered compose + secrets.
    project_dir = sandbox_cache / "agent_provisioning" / "sandboxes" / "stacks" / "khala-sbx-blog"
    assert (project_dir / "docker-compose.yml").exists()
    secrets_file = project_dir / "agent.env"
    assert secrets_file.exists()
    assert (secrets_file.stat().st_mode & 0o777) == 0o400

    body = secrets_file.read_text(encoding="utf-8")
    assert "POSTGRES_PASSWORD=" in body  # randomly generated, value irrelevant
    assert "OLLAMA_API_KEY=ollama-xyz\n" in body
    assert "ANTHROPIC_API_KEY" not in body  # absent from host env → not written

    # Rendered compose carries the agent id + the same Postgres password
    # plumbed through to both postgres and temporal services.
    rendered = (project_dir / "docker-compose.yml").read_text(encoding="utf-8")
    assert 'SANDBOX_AGENT_ID: "blogging.planner"' in rendered
    assert "khala-sbx-blog-agent" in rendered
    assert "khala-sbx-blog-postgres" in rendered
    assert "khala-sbx-blog-temporal" in rendered
    assert "khala-sbx-blog-prometheus" in rendered
    assert "khala-sbx-blog-grafana" in rendered

    # The injected agent manifest is written (0644, so the container's non-root
    # user can read the bind mount) and the compose file mounts it read-only.
    import json as _json

    manifest_file = project_dir / "agent-manifest.json"
    assert manifest_file.exists()
    assert (manifest_file.stat().st_mode & 0o777) == 0o644
    injected = _json.loads(manifest_file.read_text(encoding="utf-8"))
    assert injected["id"] == "blogging.planner"
    assert "SANDBOX_AGENT_MANIFEST_FILE: /run/agent-manifest.json" in rendered
    assert "/run/agent-manifest.json" in rendered
    assert str(manifest_file) in rendered


@pytest.mark.asyncio
async def test_run_container_cleans_up_when_compose_up_fails(
    sandbox_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-zero ``docker compose up`` removes the per-project dir + secrets."""

    async def fake_exec(cmd: list[str], *, timeout_s: int = 30):
        if cmd[:2] == ["docker", "compose"]:
            return 1, "", "compose up failed"
        return 0, "", ""

    monkeypatch.setattr(provisioner_mod, "_exec", fake_exec)

    with pytest.raises(provisioner_mod.DockerError):
        # A resolvable agent so we exercise the compose-up-fails cleanup path
        # rather than the earlier unknown-agent fail-fast.
        await provisioner_mod.run_container(
            agent_id="blogging.planner",
            container_name="khala-sbx-fail",
            team="blogging",
        )

    project_dir = sandbox_cache / "agent_provisioning" / "sandboxes" / "stacks" / "khala-sbx-fail"
    assert not project_dir.exists()


@pytest.mark.asyncio
async def test_run_container_fails_fast_for_unresolvable_agent(
    sandbox_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agent id with no registry manifest raises before ``docker compose up``
    (no stack is created), rather than letting the sandbox boot and exit 3."""
    called: list[list[str]] = []

    async def fake_exec(cmd: list[str], *, timeout_s: int = 30):
        called.append(cmd)
        return 0, "", ""

    monkeypatch.setattr(provisioner_mod, "_exec", fake_exec)

    with pytest.raises(provisioner_mod.DockerError):
        await provisioner_mod.run_container(
            agent_id="does.not.exist.anywhere",
            container_name="khala-sbx-missing",
            team="blogging",
        )

    # No docker command ran and no project dir was materialised.
    assert called == []
    project_dir = (
        sandbox_cache / "agent_provisioning" / "sandboxes" / "stacks" / "khala-sbx-missing"
    )
    assert not project_dir.exists()


@pytest.mark.asyncio
async def test_stop_container_tears_down_the_compose_project(
    sandbox_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``stop_container`` resolves the container's compose project label and
    runs ``docker compose down -v`` against that project."""
    captured: list[list[str]] = []

    async def fake_exec(cmd: list[str], *, timeout_s: int = 30):
        captured.append(cmd)
        if cmd[:3] == ["docker", "inspect", "--format"]:
            return 0, "khala-sbx-blog\n", ""  # project label
        if cmd[:2] == ["docker", "compose"]:
            return 0, "", ""
        return 0, "", ""

    monkeypatch.setattr(provisioner_mod, "_exec", fake_exec)

    await provisioner_mod.stop_container("abcdef0123456789")

    down_argv = next(c for c in captured if c[:2] == ["docker", "compose"] and "down" in c)
    assert down_argv[3] == "khala-sbx-blog"
    assert "-v" in down_argv
    assert "--remove-orphans" in down_argv


@pytest.mark.asyncio
async def test_stop_container_falls_back_to_treating_input_as_project_name(
    sandbox_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The acquire-time zombie reap passes a project name (no container yet);
    ``stop_container`` must fall back to using it directly."""
    captured: list[list[str]] = []

    async def fake_exec(cmd: list[str], *, timeout_s: int = 30):
        captured.append(cmd)
        if cmd[:3] == ["docker", "inspect", "--format"]:
            return 1, "", "Error: No such object: khala-sbx-blog"
        if cmd[:2] == ["docker", "compose"]:
            return 0, "", ""
        return 0, "", ""

    monkeypatch.setattr(provisioner_mod, "_exec", fake_exec)

    await provisioner_mod.stop_container("khala-sbx-blog")

    down_argv = next(c for c in captured if c[:2] == ["docker", "compose"] and "down" in c)
    assert down_argv[3] == "khala-sbx-blog"


def test_cleanup_secrets_file_removes_project_dir(
    sandbox_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``cleanup_secrets_file`` removes the entire per-project dir, not just
    a single file. Idempotent on missing dir."""
    project_dir = sandbox_cache / "agent_provisioning" / "sandboxes" / "stacks" / "khala-sbx-x"
    project_dir.mkdir(parents=True)
    (project_dir / "docker-compose.yml").write_text("")
    (project_dir / "agent.env").write_text("POSTGRES_PASSWORD=x")

    provisioner_mod.cleanup_secrets_file("khala-sbx-x")
    assert not project_dir.exists()

    # Second call is a no-op.
    provisioner_mod.cleanup_secrets_file("khala-sbx-x")


def test_secrets_file_does_not_carry_global_postgres_creds(
    sandbox_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-sandbox stacks own their Postgres — the host's ``POSTGRES_*`` env
    vars must NOT bleed into the agent's secrets file."""
    # Sentinel values; hyphenated so secret-scanners don't flag them as
    # credential-shaped literals. We only need to assert these strings
    # don't appear in the rendered secrets file.
    monkeypatch.setenv("POSTGRES_USER", "host-user-sentinel")
    monkeypatch.setenv("POSTGRES_PASSWORD", "host-pw-sentinel")
    monkeypatch.setenv("POSTGRES_DB", "host-db-sentinel")

    path = provisioner_mod._write_sandbox_secrets_file(
        "khala-sbx-iso", postgres_password="freshly-minted"
    )
    body = path.read_text(encoding="utf-8")

    assert "POSTGRES_PASSWORD=freshly-minted\n" in body
    assert "host-user-sentinel" not in body
    assert "host-pw-sentinel" not in body
    assert "host-db-sentinel" not in body


# -------------------------------------------------------------------------
# Additional provisioner coverage: _exec timeouts, is_running /
# inspect_host_port, run/stop container cleanup, and asset materialisation.
# -------------------------------------------------------------------------


def _make_proc_stub():
    """Return a coroutine that resolves to a stub Process-like object."""

    async def _coro():
        proc = MagicMock()

        async def _comm():
            return (b"", b"")

        async def _wait():
            return None

        proc.communicate = _comm
        proc.kill = MagicMock()
        proc.wait = _wait
        proc.returncode = 0
        return proc

    return _coro()


@pytest.mark.asyncio
async def test_provisioner_exec_kills_on_timeout(monkeypatch) -> None:
    from agent_platform.sandbox import provisioner as pm

    # Real proc-stub that hangs in communicate until cancelled.
    proc_stub = MagicMock()
    proc_stub.kill = MagicMock()

    async def comm():
        await asyncio.sleep(10)
        return (b"", b"")

    async def wait():
        return None

    proc_stub.communicate = comm
    proc_stub.wait = wait
    proc_stub.returncode = 0

    async def fake_create(*args, **kwargs):
        return proc_stub

    monkeypatch.setattr(pm.asyncio, "create_subprocess_exec", fake_create)

    with pytest.raises(pm.DockerError, match="timed out"):
        # Wait for a very small timeout
        await pm._exec(["docker", "info"], timeout_s=0)

    proc_stub.kill.assert_called_once()


def test_materialise_project_dir_copies_grafana(tmp_path: Path, monkeypatch) -> None:
    from agent_platform.sandbox import provisioner as pm

    # sandbox_stack_assets_dir() returns the template's parent directory,
    # so colocate the support files with the template.
    template = tmp_path / "sandbox-stack.yml"
    template.write_text(
        "sandbox: {sandbox_id}\nagent: {agent_id}\nimage: {agent_image}\n"
        "pw: {pg_password}\nsecrets: {agent_secrets_file}\nport: {agent_host_port}\n"
    )
    (tmp_path / "postgres-init.sql").write_text("-- init")
    (tmp_path / "prometheus.yml").write_text("global: {}")
    grafana = tmp_path / "grafana-provisioning"
    grafana.mkdir()
    (grafana / "datasources.yml").write_text("apiVersion: 1")

    monkeypatch.setenv("AGENT_PROVISIONING_SANDBOX_STACK_TEMPLATE", str(template))
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path / "cache"))

    path = pm._materialise_project_dir(
        "khala-sbx-test", host_port=12345, postgres_password="pw", manifest_json='{"id": "x"}'
    )
    assert (path / "docker-compose.yml").exists()
    assert (path / "grafana-provisioning" / "datasources.yml").exists()
    # Re-run should overwrite (tests the grafana_dst exists rmtree branch).
    pm._materialise_project_dir(
        "khala-sbx-test", host_port=12345, postgres_password="pw", manifest_json='{"id": "x"}'
    )


@pytest.mark.asyncio
async def test_exec_handles_timeout(monkeypatch) -> None:
    from agent_platform.sandbox import provisioner as pm

    async def fake_create_subprocess_exec(*args, **kwargs):
        # Simulate a stuck subprocess by returning a proc whose communicate
        # never resolves before our timeout fires.
        proc = MagicMock()
        proc.communicate = MagicMock(return_value=asyncio.Future())
        proc.kill = MagicMock()
        proc.wait = MagicMock(return_value=asyncio.Future())
        proc.returncode = 1
        # Make wait_for actually time out
        proc.communicate.return_value.set_exception(asyncio.TimeoutError())
        proc.wait.return_value.set_result(None)
        return proc

    # Easier: patch asyncio.wait_for to raise TimeoutError directly.
    monkeypatch.setattr(
        pm.asyncio,
        "create_subprocess_exec",
        lambda *a, **kw: _make_proc_stub(),
    )

    with patch.object(pm.asyncio, "wait_for", side_effect=asyncio.TimeoutError()):
        with pytest.raises(pm.DockerError, match="timed out"):
            await pm._exec(["docker", "info"], timeout_s=1)


@pytest.mark.asyncio
async def test_is_running_true(monkeypatch) -> None:
    from unittest.mock import AsyncMock as _AsyncMock

    from agent_platform.sandbox import provisioner as pm

    monkeypatch.setattr(pm, "_exec", _AsyncMock(return_value=(0, "true\n", "")))
    assert await pm.is_running("c1") is True


@pytest.mark.asyncio
async def test_is_running_false(monkeypatch) -> None:
    from unittest.mock import AsyncMock as _AsyncMock

    from agent_platform.sandbox import provisioner as pm

    monkeypatch.setattr(pm, "_exec", _AsyncMock(return_value=(0, "false\n", "")))
    assert await pm.is_running("c1") is False


@pytest.mark.asyncio
async def test_is_running_returns_false_on_error(monkeypatch) -> None:
    from unittest.mock import AsyncMock as _AsyncMock

    from agent_platform.sandbox import provisioner as pm

    monkeypatch.setattr(pm, "_exec", _AsyncMock(return_value=(1, "", "no such container")))
    assert await pm.is_running("c1") is False


@pytest.mark.asyncio
async def test_inspect_host_port_raises_on_invalid_output(monkeypatch) -> None:
    from unittest.mock import AsyncMock as _AsyncMock

    from agent_platform.sandbox import provisioner as pm

    monkeypatch.setattr(pm, "_exec", _AsyncMock(return_value=(0, "not-a-port\n", "")))
    with pytest.raises(pm.DockerError):
        await pm.inspect_host_port("c1")


@pytest.mark.asyncio
async def test_inspect_host_port_raises_on_nonzero(monkeypatch) -> None:
    from unittest.mock import AsyncMock as _AsyncMock

    from agent_platform.sandbox import provisioner as pm

    monkeypatch.setattr(pm, "_exec", _AsyncMock(return_value=(1, "", "boom")))
    with pytest.raises(pm.DockerError):
        await pm.inspect_host_port("c1")


@pytest.mark.asyncio
async def test_inspect_host_port_success(monkeypatch) -> None:
    from unittest.mock import AsyncMock as _AsyncMock

    from agent_platform.sandbox import provisioner as pm

    monkeypatch.setattr(pm, "_exec", _AsyncMock(return_value=(0, "55123\n", "")))
    port = await pm.inspect_host_port("c1")
    assert port == 55123


@pytest.mark.asyncio
async def test_run_container_cleans_up_on_inspect_failure(tmp_path: Path, monkeypatch) -> None:
    """If `docker inspect` fails after compose up, project dir is cleaned up."""
    from agent_platform.sandbox import provisioner as pm

    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))

    async def fake_exec(cmd, *, timeout_s=30):
        if cmd[:2] == ["docker", "compose"]:
            return 0, "", ""
        if cmd[:2] == ["docker", "inspect"]:
            return 1, "", "no such container"
        return 0, "", ""

    monkeypatch.setattr(pm, "_exec", fake_exec)
    # Stub manifest resolution so the test reaches the compose-up → inspect-fail
    # path (the cleanup branch under test) regardless of the on-disk registry's
    # contents — otherwise an unresolvable agent id would fail fast in
    # ``_resolve_manifest_json`` before the project dir is ever created, and the
    # ``not project_dir.exists()`` assertion would pass vacuously (false positive).
    from unittest.mock import AsyncMock as _AsyncMock

    monkeypatch.setattr(pm, "_resolve_manifest_json", _AsyncMock(return_value="{}"))

    with pytest.raises(pm.DockerError):
        await pm.run_container(
            agent_id="test.agent", container_name="khala-sbx-inspect-fail", team="x"
        )

    # Project dir must be removed on cleanup.
    project_dir = (
        tmp_path / "agent_provisioning" / "sandboxes" / "stacks" / "khala-sbx-inspect-fail"
    )
    assert not project_dir.exists()


@pytest.mark.asyncio
async def test_run_container_cleans_up_on_docker_error_in_exec(tmp_path: Path, monkeypatch) -> None:
    from agent_platform.sandbox import provisioner as pm

    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))

    async def boom(cmd, *, timeout_s=30):
        if cmd[:2] == ["docker", "compose"]:
            raise pm.DockerError("compose timed out")
        return 0, "", ""

    monkeypatch.setattr(pm, "_exec", boom)
    # See the inspect-failure test above: stub manifest resolution so we reach the
    # compose-up failure (the cleanup path) rather than the unknown-agent fail-fast.
    from unittest.mock import AsyncMock as _AsyncMock

    monkeypatch.setattr(pm, "_resolve_manifest_json", _AsyncMock(return_value="{}"))

    with pytest.raises(pm.DockerError):
        await pm.run_container(agent_id="test.agent", container_name="khala-sbx-boom", team="x")

    project_dir = tmp_path / "agent_provisioning" / "sandboxes" / "stacks" / "khala-sbx-boom"
    assert not project_dir.exists()


@pytest.mark.asyncio
async def test_stop_container_raises_for_unexpected_failure(monkeypatch) -> None:

    from agent_platform.sandbox import provisioner as pm

    async def fake_exec(cmd, *, timeout_s=30):
        if cmd[:3] == ["docker", "inspect", "--format"]:
            return 1, "", "no such container"
        if cmd[:2] == ["docker", "compose"]:
            # Real failure (not "no such")
            return 1, "", "unexpected error from compose"
        return 0, "", ""

    monkeypatch.setattr(pm, "_exec", fake_exec)
    with pytest.raises(pm.DockerError):
        await pm.stop_container("khala-sbx-error")


def test_ensure_network_is_noop_shim() -> None:
    from agent_platform.sandbox import provisioner as pm

    asyncio.run(pm.ensure_network())


def test_cleanup_secrets_file_swallows_oserror(tmp_path: Path, monkeypatch) -> None:
    """rmtree failures are logged but not raised."""
    from agent_platform.sandbox import provisioner as pm

    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    project_dir = pm.sandbox_project_dir("khala-sbx-x")
    project_dir.mkdir(parents=True)
    (project_dir / "agent.env").write_text("X=1")

    with patch.object(pm.shutil, "rmtree", side_effect=OSError("io")):
        pm.cleanup_secrets_file("khala-sbx-x")


# -------------------------------------------------------------------------
# Container naming and stop_container (module-level provisioner helpers).
# -------------------------------------------------------------------------


def test_container_name_is_dns_safe() -> None:
    name = container_name_for("blogging.planner")
    assert name.startswith("khala-sbx-blogging.planner-")
    # readable prefix + 8 lowercase-hex char digest suffix.
    suffix = name.rsplit("-", 1)[1]
    assert len(suffix) == 8
    assert all(c in "0123456789abcdef" for c in suffix)
    # Deterministic.
    assert container_name_for("blogging.planner") == name
    # Empty id still yields a valid container name.
    assert container_name_for("").startswith("khala-sbx-agent-")


def test_container_name_is_collision_resistant_under_sanitization() -> None:
    # Two ids that sanitize to the same readable prefix still get distinct
    # container names, so the acquire-time zombie reap cannot accidentally
    # tear down another agent's live sandbox.
    assert container_name_for("agent/1") != container_name_for("agent-1")
    assert container_name_for("a b") != container_name_for("a-b")


@pytest.mark.asyncio
async def test_stop_container_is_idempotent_on_missing_container() -> None:
    with patch.object(
        provisioner_mod,
        "_exec",
        new=AsyncMock(return_value=(1, "", "Error: No such container: abc")),
    ):
        await provisioner_mod.stop_container("abc")  # must not raise


@pytest.mark.asyncio
async def test_stop_container_raises_on_real_failure() -> None:
    with patch.object(
        provisioner_mod,
        "_exec",
        new=AsyncMock(return_value=(1, "", "Cannot connect to the Docker daemon")),
    ):
        with pytest.raises(provisioner_mod.DockerError):
            await provisioner_mod.stop_container("abc")
