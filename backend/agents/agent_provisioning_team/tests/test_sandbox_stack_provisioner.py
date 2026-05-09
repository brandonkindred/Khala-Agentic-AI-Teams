"""Tests for the per-sandbox compose-stack provisioner (#456).

The provisioner shells out to ``docker compose`` to bring up a self-
contained stack (postgres + temporal + prometheus + grafana + agent) per
sandbox. Tests stub ``_exec`` so no real Docker daemon is required and
assert on the rendered compose file + the argv shape used to bring the
stack up and tear it down.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_provisioning_team.sandbox import provisioner as provisioner_mod


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
        await provisioner_mod.run_container(
            agent_id="agent-1",
            container_name="khala-sbx-fail",
            team="blogging",
        )

    project_dir = sandbox_cache / "agent_provisioning" / "sandboxes" / "stacks" / "khala-sbx-fail"
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
    monkeypatch.setenv("POSTGRES_USER", "host_user")
    monkeypatch.setenv("POSTGRES_PASSWORD", "host_pw")
    monkeypatch.setenv("POSTGRES_DB", "host_db")

    path = provisioner_mod._write_sandbox_secrets_file(
        "khala-sbx-iso", postgres_password="freshly-minted"
    )
    body = path.read_text(encoding="utf-8")

    assert "POSTGRES_PASSWORD=freshly-minted\n" in body
    assert "host_user" not in body
    assert "host_pw" not in body
    assert "host_db" not in body
