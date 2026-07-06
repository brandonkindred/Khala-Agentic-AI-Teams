"""Per-sandbox docker-compose stack lifecycle.

Each agent run gets its own self-contained compose project: the agent
container plus a sandbox-internal Postgres, Temporal, Prometheus, and
Grafana. No service in this stack joins the long-lived ``khala-stack``
compose network — every supporting service runs *inside* the sandbox so
the agent can be tested as if it were in a live environment without
touching anything outside.

The lifecycle layer (``sandbox/lifecycle.py``) calls these helpers via
the same public surface that the previous single-container provisioner
exposed: ``run_container`` brings a stack up, ``inspect_host_port``
resolves the agent's exposed port, ``stop_container`` tears the stack
down. ``container_id`` is reused as the *agent* container's id so the
existing state schema (``SandboxState.container_id``) survives.

History: this module previously launched a single hardened container on
the shared ``khala-sandbox`` bridge and forwarded credentials to whatever
Postgres/Temporal happened to be running on the host. That model was
replaced (#456) when the project moved to single-operator personal use:
"one environment per sandbox, fully isolated, fully equipped."
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
from pathlib import Path

from .state import (
    sandbox_image,
    sandbox_project_dir,
    sandbox_stack_assets_dir,
    sandbox_stack_template_path,
)

logger = logging.getLogger(__name__)

# Non-sensitive host env vars forwarded into the *agent* container. The
# stack itself is parameterised via the rendered compose file; everything
# secret (Postgres password, *_API_KEY) flows through the 0400 secrets
# file bind-mounted into the agent service only.
_FORWARDED_ENV = (
    "LLM_PROVIDER",
    "LLM_BASE_URL",
    "LLM_MODEL",
)

_CONTAINER_NAME_RE = re.compile(r"[^a-zA-Z0-9_.-]+")


class DockerError(RuntimeError):
    """Raised when a docker / docker-compose CLI invocation exits non-zero."""


def _fail(argv: list[str], rc: int, stderr: str) -> DockerError:
    return DockerError(f"docker failed (exit {rc}): {' '.join(argv)}\n{stderr}")


def _secrets_host_path(project_name: str) -> Path:
    """Deterministic 0400 secrets file path for ``project_name``.

    Lives next to the per-sandbox compose project directory so a single
    ``shutil.rmtree`` of that directory cleans up the whole stack's host
    footprint at teardown.
    """
    return sandbox_project_dir(project_name) / "agent.env"


def _write_sandbox_secrets_file(project_name: str, *, postgres_password: str) -> Path:
    """Atomically write the 0400 ``KEY=VALUE`` secrets file for the agent.

    Picks up host-side ``OLLAMA_API_KEY`` / ``ANTHROPIC_API_KEY`` so the
    agent can still call the cloud LLM provider — those are external APIs,
    not part of the "no shared services" rule. The freshly minted Postgres
    password is the only sandbox-internal secret on this path.
    """
    values: dict[str, str] = {
        "POSTGRES_PASSWORD": postgres_password,
    }
    for key in ("OLLAMA_API_KEY", "ANTHROPIC_API_KEY"):
        v = os.environ.get(key)
        if v is not None:
            values[key] = v
    for key in _FORWARDED_ENV:
        v = os.environ.get(key)
        if v is not None:
            values[key] = v

    path = _secrets_host_path(project_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"{k}={v}\n" for k, v in values.items())
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.chmod(tmp, 0o400)
    os.replace(tmp, path)
    return path


def _manifest_host_path(project_name: str) -> Path:
    """Deterministic path for the injected agent-manifest bind-mount source.

    Lives in the per-sandbox project directory alongside the compose file and
    secrets file, so a single ``rmtree`` of that directory cleans it up.
    """
    return sandbox_project_dir(project_name) / "agent-manifest.json"


def _write_manifest_file(project_name: str, manifest_json: str) -> Path:
    """Write the platform-authored agent manifest for the sandbox to bind-mount.

    The entrypoint reads this (``SANDBOX_AGENT_MANIFEST_FILE``) and registers it so
    a dynamically-registered agent — absent from the sandbox image's on-disk
    registry — can still boot. Platform-authored, carries no secrets; mounted
    read-only into the agent container.

    Preconditions:
        * ``manifest_json`` is a JSON object string (an ``AgentManifest`` dump).
    Postconditions:
        * The file exists at :func:`_manifest_host_path` and is world-readable
          (0644) so the container's non-root user can read the bind mount.
    """
    path = _manifest_host_path(project_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    # Create with the target mode atomically (no separate chmod window) — umask
    # is still applied to this request, so still explicitly chmod after to
    # guarantee 0644 regardless of the process's umask.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(manifest_json)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    return path


async def _resolve_manifest_json(agent_id: str) -> str:
    """Serialize the registered manifest for ``agent_id`` to a JSON string.

    Preconditions:
        * ``agent_id`` resolves in the process-wide registry (the lifecycle's
          ``_resolve_team`` already validated this before ``run_container``).
    Postconditions:
        * Returns ``json.dumps(manifest.model_dump(mode="json"))``. Runs the
          (possibly Postgres-backed) registry lookup in a worker thread via
          ``asyncio.to_thread`` so it never blocks this coroutine's event loop.
    Raises:
        * :class:`DockerError` if the agent is unexpectedly unresolvable — fail
          fast here, before ``docker compose up``, rather than let the sandbox
          boot and exit ``EXIT_UNKNOWN_AGENT``.
    """
    from agent_registry import get_registry

    manifest = await asyncio.to_thread(get_registry().get, agent_id)
    if manifest is None:
        raise DockerError(
            f"Cannot provision sandbox for {agent_id!r}: no manifest resolved in the registry."
        )
    return json.dumps(manifest.model_dump(mode="json"))


def cleanup_secrets_file(project_name: str) -> None:
    """Idempotently remove the per-sandbox project directory + secrets.

    Kept under the historical name so callers (lifecycle, tests) don't need
    to change. Removes the entire on-disk footprint for ``project_name``.
    """
    path = sandbox_project_dir(project_name)
    if not path.exists():
        return
    try:
        shutil.rmtree(path)
    except OSError as exc:
        logger.warning("Could not remove sandbox project directory for %s: %s", project_name, exc)


def container_name_for(agent_id: str) -> str:
    """Deterministic, DNS-safe, collision-resistant compose-project name.

    Reused as both the compose project name (passed to ``docker compose -p``)
    and the agent container's ``container_name`` (suffixed with ``-agent`` by
    the compose template). The 8-char sha1 suffix keeps the mapping
    one-to-one even when two ids would otherwise sanitise the same way.
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


def _allocate_host_port() -> int:
    """Pick an unused loopback port for the agent's published 8090/tcp.

    The compose ``ports`` mapping treats this as a request — the kernel
    rebinds it via the bound socket, so a brief race window where two
    sandboxes pick the same port is harmless (compose-up will simply fail
    and retry). For first-cut simplicity we use ``socket.bind`` to ask the
    OS for a free port instead.
    """
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


def _materialise_project_dir(
    project_name: str, *, host_port: int, postgres_password: str, manifest_json: str
) -> Path:
    """Render the compose template into a fresh per-project directory.

    Copies the support assets (postgres-init.sql, prometheus.yml,
    grafana-provisioning/) alongside so the rendered compose file's
    relative volume paths resolve. Also writes the injected agent-manifest
    JSON that the ``{agent_manifest_file}`` bind-mount points at. Returns the
    project directory.
    """
    project_dir = sandbox_project_dir(project_name)
    project_dir.mkdir(parents=True, exist_ok=True)

    assets = sandbox_stack_assets_dir()
    for asset in ("postgres-init.sql", "prometheus.yml"):
        src = assets / asset
        if src.exists():
            shutil.copy2(src, project_dir / asset)

    # Grafana provisioning is a directory tree.
    grafana_src = assets / "grafana-provisioning"
    grafana_dst = project_dir / "grafana-provisioning"
    if grafana_dst.exists():
        shutil.rmtree(grafana_dst)
    if grafana_src.exists():
        shutil.copytree(grafana_src, grafana_dst)

    secrets_path = _write_sandbox_secrets_file(project_name, postgres_password=postgres_password)
    manifest_path = _write_manifest_file(project_name, manifest_json)

    template = sandbox_stack_template_path().read_text(encoding="utf-8")
    rendered = template.format(
        sandbox_id=project_name,
        agent_id=os.environ.get("_RENDER_AGENT_ID", "<agent-id-placeholder>"),
        agent_image=sandbox_image(),
        pg_password=postgres_password,
        agent_secrets_file=str(secrets_path),
        agent_manifest_file=str(manifest_path),
        agent_host_port=host_port,
    )
    (project_dir / "docker-compose.yml").write_text(rendered, encoding="utf-8")
    return project_dir


async def run_container(agent_id: str, container_name: str, team: str) -> str:
    """Bring up the per-sandbox compose stack for ``agent_id``.

    ``container_name`` doubles as the compose project name. Returns the
    agent container's id so :class:`SandboxState.container_id` keeps the
    same shape as in the single-container era. ``team`` is unused now (we
    used to look up team-scoped Postgres credentials there) but kept on
    the signature so the lifecycle's call site doesn't change.
    """
    project_name = container_name
    postgres_password = secrets.token_urlsafe(24)
    host_port = _allocate_host_port()

    # Resolve + serialize the manifest to inject into the isolated sandbox (which
    # boots from its own on-disk registry and can't reach the platform Postgres).
    # Fail fast here, before ``docker compose up``, if the agent is unresolvable.
    manifest_json = await _resolve_manifest_json(agent_id)

    # Render the project dir with the resolved agent_id so the agent
    # container's SANDBOX_AGENT_ID env var is correct.
    os.environ["_RENDER_AGENT_ID"] = agent_id
    try:
        project_dir = _materialise_project_dir(
            project_name,
            host_port=host_port,
            postgres_password=postgres_password,
            manifest_json=manifest_json,
        )
    finally:
        os.environ.pop("_RENDER_AGENT_ID", None)

    compose_file = project_dir / "docker-compose.yml"
    argv = [
        "docker",
        "compose",
        "-p",
        project_name,
        "-f",
        str(compose_file),
        "up",
        "-d",
        "--remove-orphans",
    ]
    try:
        # Compose pulls images + waits for service-healthy depends_on. Allow
        # plenty of time for the cold path on first use.
        rc, stdout, stderr = await _exec(argv, timeout_s=180)
    except DockerError:
        cleanup_secrets_file(project_name)
        raise
    if rc != 0:
        cleanup_secrets_file(project_name)
        raise _fail(argv, rc, stderr or stdout)

    agent_container = f"{project_name}-agent"
    inspect_argv = [
        "docker",
        "inspect",
        "--format",
        "{{.Id}}",
        agent_container,
    ]
    rc2, container_id, err = await _exec(inspect_argv)
    if rc2 != 0 or not container_id.strip():
        cleanup_secrets_file(project_name)
        raise _fail(inspect_argv, rc2, err or container_id)
    return container_id.strip()


async def inspect_host_port(container_id: str) -> int:
    """Resolve the host-side loopback port that maps to the agent's ``8090/tcp``.

    Looks up the agent container directly (compose's ``-p`` project name
    propagates through to the container name suffix, but ``container_id``
    is opaque to compose so we just inspect by id).
    """
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
    """Return True iff ``docker inspect`` reports the agent container as running.

    Missing / removed containers return False (not an error).
    """
    rc, stdout, _ = await _exec(
        ["docker", "inspect", "--format", "{{.State.Running}}", container_id],
    )
    return rc == 0 and stdout.strip().lower() == "true"


async def stop_container(container_or_project: str) -> None:
    """Tear down the entire compose project for ``container_or_project``.

    The lifecycle calls this in two shapes:

    * With an **agent container id** during explicit teardown — mirrors
      the pre-compose API. We resolve the container id back to its compose
      project label and tear down by project name.
    * With a **compose project name** during the acquire-time zombie
      reap, before any container exists. We treat the argument as a
      project name directly.

    Either way, the result is ``docker compose -p <project> down -v``,
    which removes the entire stack including named volumes. Missing
    containers / projects are idempotent successes.
    """
    project: str | None = None

    # Try id-shaped lookup first; the compose-label query returns rc=0 with
    # an empty label for non-compose containers (none in our world) and
    # rc!=0 for "no such container", so we fall back to treating the arg
    # as a project name.
    rc, project_label, _ = await _exec(
        [
            "docker",
            "inspect",
            "--format",
            '{{ index .Config.Labels "com.docker.compose.project" }}',
            container_or_project,
        ]
    )
    if rc == 0 and project_label.strip():
        project = project_label.strip()
    else:
        # Treat the input as a compose project name directly. If the
        # project doesn't exist, ``compose down`` reports nothing to remove
        # and exits 0 — same idempotent semantic as the id path.
        project = container_or_project

    project_dir = sandbox_project_dir(project)
    compose_file = project_dir / "docker-compose.yml"

    argv = [
        "docker",
        "compose",
        "-p",
        project,
    ]
    if compose_file.exists():
        argv += ["-f", str(compose_file)]
    argv += ["down", "-v", "--remove-orphans"]

    rc2, _, stderr = await _exec(argv, timeout_s=120)
    if rc2 != 0 and "no such" not in stderr.lower():
        raise _fail(argv, rc2, stderr)

    cleanup_secrets_file(project)


# Backwards-compat shim: the previous module exposed ``ensure_network`` so the
# lifecycle could create the shared ``khala-sandbox`` bridge on demand. Each
# compose project now owns its own bridge, so this is a no-op kept around so
# imports that haven't been updated yet don't break.
async def ensure_network() -> None:  # pragma: no cover — trivial shim
    return None
