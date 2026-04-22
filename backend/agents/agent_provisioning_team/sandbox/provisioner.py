"""Thin asyncio wrapper around ``docker run``/``docker inspect``/``docker rm``.

Module-level coroutines (not a class) so tests can patch them cleanly via
``patch("agent_provisioning_team.sandbox.provisioner.run_container", ...)``.
Runs one ephemeral container per agent.

The hardening flags from issue #255 (cap-drop, read-only, security-opt,
resource caps, loopback-bound ports) are assembled here rather than in the
shared ``tool_agents/docker_provisioner.py`` so the existing tool stays
unchanged for the non-sandbox provisioning path.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from pathlib import Path

from .state import sandbox_image, sandbox_network

logger = logging.getLogger(__name__)

# Non-sensitive host env vars forwarded into the sandbox via `-e KEY=VALUE`.
# Secrets (POSTGRES_USER/PASSWORD/DB, *_API_KEY) are NEVER on this path — they
# flow through a 0400 bind-mounted file the in-sandbox loader reads once at
# startup. See `_write_sandbox_secrets_file` and issue #257.
_FORWARDED_ENV = (
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "LLM_PROVIDER",
    "LLM_BASE_URL",
    "LLM_MODEL",
)

# In-sandbox path where the per-sandbox secrets file is bind-mounted read-only.
_SANDBOX_SECRETS_TARGET = "/run/secrets/sandbox-env"

_CONTAINER_NAME_RE = re.compile(r"[^a-zA-Z0-9_.-]+")


class DockerError(RuntimeError):
    """Raised when a docker CLI invocation exits non-zero."""


def _fail(argv: list[str], rc: int, stderr: str) -> DockerError:
    return DockerError(f"docker failed (exit {rc}): {' '.join(argv)}\n{stderr[:500]}")


def _secrets_host_path(container_name: str) -> Path:
    """Deterministic host path for a sandbox's 0400 secrets file.

    Keyed off ``container_name`` so teardown can clean it up without needing to
    remember the path separately. Lives under ``AGENT_CACHE`` for parity with
    the rest of the provisioning state.
    """
    cache = os.environ.get("AGENT_CACHE", "/tmp/agents")
    return Path(cache) / "agent_provisioning" / "sandboxes" / "secrets" / f"{container_name}.env"


def _team_postgres_credentials(team: str) -> tuple[str | None, str | None, str | None]:
    """Resolve team-scoped ``(user, password, db)`` for a sandbox's Postgres creds.

    Reads ``POSTGRES_PASSWORD_SANDBOX_<TEAM>`` and uses the convention
    ``sandbox_<team>`` for both role and database. If the team-scoped password
    is unset, falls back to the global ``POSTGRES_USER`` / ``POSTGRES_PASSWORD``
    / ``POSTGRES_DB`` with a warning so local dev keeps working when per-team
    roles haven't been provisioned yet.
    """
    env_key = f"POSTGRES_PASSWORD_SANDBOX_{team.upper()}"
    password = os.environ.get(env_key)
    if password:
        return (f"sandbox_{team}", password, f"sandbox_{team}")
    logger.warning(
        "No %s set; sandbox for team %r will fall back to the global "
        "POSTGRES_USER/PASSWORD/DB. See docker/README.md § Per-team Postgres "
        "isolation.",
        env_key,
        team,
    )
    return (
        os.environ.get("POSTGRES_USER"),
        os.environ.get("POSTGRES_PASSWORD"),
        os.environ.get("POSTGRES_DB"),
    )


def _write_sandbox_secrets_file(container_name: str, team: str) -> Path:
    """Atomically write a 0400 ``KEY=VALUE`` file with the sandbox's secrets.

    The file is bind-mounted read-only at :data:`_SANDBOX_SECRETS_TARGET` in
    the sandbox, where the entrypoint loader reads it into ``os.environ`` and
    unlinks the in-sandbox view. Values absent from the host environment are
    simply omitted — the loader treats missing keys as no-ops.
    """
    pg_user, pg_pass, pg_db = _team_postgres_credentials(team)
    values: dict[str, str] = {}
    if pg_user is not None:
        values["POSTGRES_USER"] = pg_user
    if pg_pass is not None:
        values["POSTGRES_PASSWORD"] = pg_pass
    if pg_db is not None:
        values["POSTGRES_DB"] = pg_db
    for key in ("OLLAMA_API_KEY", "ANTHROPIC_API_KEY"):
        v = os.environ.get(key)
        if v is not None:
            values[key] = v

    path = _secrets_host_path(container_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"{k}={v}\n" for k, v in values.items())
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.chmod(tmp, 0o400)
    os.replace(tmp, path)
    return path


def cleanup_secrets_file(container_name: str) -> None:
    """Remove the host-side sandbox secrets file for ``container_name``.

    Idempotent: missing-file is treated as success. Called from the lifecycle
    after ``docker rm -f`` succeeds and before provisioning a fresh sandbox
    so stale creds don't linger if the host process was killed between
    sandbox teardowns.
    """
    try:
        _secrets_host_path(container_name).unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning("Could not remove sandbox secrets file for %s: %s", container_name, exc)


def container_name_for(agent_id: str) -> str:
    """Deterministic, DNS-safe, collision-resistant container name for ``agent_id``.

    Docker requires ``[a-zA-Z0-9][a-zA-Z0-9_.-]*``. The readable prefix is the
    sanitised agent id (truncated); the 8-char sha1 suffix keeps the mapping
    one-to-one so two ids that happen to sanitise the same way (e.g.
    ``agent/1`` vs ``agent-1``) still get distinct container names — the
    acquire-time zombie reap would otherwise tear down a sibling's live
    container.
    """
    safe = (_CONTAINER_NAME_RE.sub("-", agent_id).strip("-") or "agent")[:40]
    digest = hashlib.sha1(agent_id.encode("utf-8")).hexdigest()[:8]
    return f"khala-sbx-{safe}-{digest}"


async def _exec(cmd: list[str], *, timeout_s: int = 30) -> tuple[int, str, str]:
    logger.debug("exec: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise DockerError(f"docker timed out after {timeout_s}s: {' '.join(cmd)}") from None
    return (
        proc.returncode or 0,
        stdout_b.decode("utf-8", errors="replace"),
        stderr_b.decode("utf-8", errors="replace"),
    )


def _build_run_argv(
    *,
    agent_id: str,
    container_name: str,
    secrets_host_path: Path | None = None,
) -> list[str]:
    """Assemble the hardened ``docker run`` argument vector for a sandbox.

    Kept as a pure function so tests can assert on the exact flags without
    invoking subprocess. When ``secrets_host_path`` is provided, the file is
    bind-mounted read-only at :data:`_SANDBOX_SECRETS_TARGET` and the sandbox
    is told where to find it via ``SANDBOX_SECRETS_FILE``.
    """
    argv: list[str] = [
        "docker",
        "run",
        "-d",
        "--rm",
        "--name",
        container_name,
        "--hostname",
        container_name,
        "--network",
        sandbox_network(),
        # `127.0.0.1::8090` binds to loopback only; Docker picks a free host port.
        "-p",
        "127.0.0.1::8090",
        "--cpus=1.0",
        "--memory=1g",
        "--pids-limit=512",
        "--ulimit",
        "nproc=1024",
        "--ulimit",
        "nofile=4096",
        "--security-opt=no-new-privileges:true",
        "--security-opt=seccomp=default",
        "--cap-drop=ALL",
        "--read-only",
        "--tmpfs",
        "/tmp",
        "--tmpfs",
        "/run",
        # Phase 1 entrypoint binds the sandbox to exactly this agent id.
        "-e",
        f"SANDBOX_AGENT_ID={agent_id}",
    ]
    for key in _FORWARDED_ENV:
        value = os.environ.get(key)
        if value is not None:
            argv.extend(["-e", f"{key}={value}"])
    if secrets_host_path is not None:
        argv.extend(
            [
                "--mount",
                f"type=bind,source={secrets_host_path},target={_SANDBOX_SECRETS_TARGET},readonly",
                "-e",
                f"SANDBOX_SECRETS_FILE={_SANDBOX_SECRETS_TARGET}",
            ]
        )
    argv.append(sandbox_image())
    return argv


async def ensure_network() -> None:
    """Idempotently ensure the sandbox bridge network exists.

    The per-agent lifecycle runs ``docker run`` directly rather than
    ``docker compose``, so nothing creates the bridge implicitly — we do it
    here on demand. No-op when the network already exists.
    """
    name = sandbox_network()
    rc, _, _ = await _exec(["docker", "network", "inspect", name], timeout_s=15)
    if rc == 0:
        return
    argv = ["docker", "network", "create", "--driver", "bridge", name]
    rc2, stdout, stderr = await _exec(argv, timeout_s=30)
    if rc2 != 0 and "already exists" not in (stderr + stdout).lower():
        raise _fail(argv, rc2, stderr or stdout)


async def run_container(agent_id: str, container_name: str, team: str) -> str:
    """Start a hardened sandbox for ``agent_id`` and return its container id.

    Writes a per-sandbox 0400 secrets file (team-scoped Postgres creds +
    any ``*_API_KEY`` values present on the host) and bind-mounts it read-only
    into the container — secrets are never passed via ``-e`` flags.

    Caller is responsible for polling ``/health`` before treating the sandbox
    as ready — this coroutine returns as soon as the Docker daemon accepts
    the container, not when uvicorn is listening.
    """
    await ensure_network()
    secrets_host_path = _write_sandbox_secrets_file(container_name, team)
    argv = _build_run_argv(
        agent_id=agent_id,
        container_name=container_name,
        secrets_host_path=secrets_host_path,
    )
    try:
        rc, stdout, stderr = await _exec(argv, timeout_s=60)
    except DockerError:
        cleanup_secrets_file(container_name)
        raise
    if rc != 0:
        cleanup_secrets_file(container_name)
        raise _fail(argv, rc, stderr or stdout)
    container_id = stdout.strip()
    if not container_id:
        cleanup_secrets_file(container_name)
        raise _fail(argv, rc, "docker run printed no container id")
    return container_id


async def inspect_host_port(container_id: str) -> int:
    """Resolve the host-side loopback port that maps to the sandbox's ``8090/tcp``."""
    argv = [
        "docker",
        "inspect",
        "--format",
        '{{ (index (index .NetworkSettings.Ports "8090/tcp") 0).HostPort }}',
        container_id,
    ]
    rc, stdout, stderr = await _exec(argv)
    if rc != 0:
        raise _fail(argv, rc, stderr or stdout)
    port_str = stdout.strip()
    if not port_str.isdigit():
        raise _fail(argv, rc, f"could not parse host port from {stdout!r}")
    return int(port_str)


async def is_running(container_id: str) -> bool:
    """Return True iff ``docker inspect`` reports the container as running.

    Missing / removed containers return False (not an error).
    """
    rc, stdout, _ = await _exec(
        ["docker", "inspect", "--format", "{{.State.Running}}", container_id],
    )
    return rc == 0 and stdout.strip().lower() == "true"


async def stop_container(container_id: str) -> None:
    """Stop and remove ``container_id``.

    ``docker rm -f`` stops running containers before removing them, so a single
    call covers both the happy path and the zombie-container case. Missing
    containers are treated as idempotent success; any other non-zero exit
    (daemon unreachable, permissions, etc.) raises :class:`DockerError` so
    callers don't silently evict state while the container is still alive.
    """
    argv = ["docker", "rm", "-f", container_id]
    rc, _, stderr = await _exec(argv)
    if rc == 0:
        return
    if "no such container" in stderr.lower():
        return
    raise _fail(argv, rc, stderr)
