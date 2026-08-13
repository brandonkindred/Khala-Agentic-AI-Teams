"""Single-agent sandbox bootstrap.

Phase 1 of the sandbox re-architecture (issue #263). Loads exactly one AI
agent — identified by ``SANDBOX_AGENT_ID`` — and exposes it via
``POST /_agents/{agent_id}/invoke`` plus ``GET /health`` on ``0.0.0.0:8090``.

The module is the container ``CMD``; it never runs in the unified API process.
Invariants:
  * Must not write to ``/app`` at runtime (image will be run ``--read-only``).
  * Fail fast with non-zero exit codes so the lifecycle owner can observe failures.
  * Only the single bound ``SANDBOX_AGENT_ID`` is invocable. Requests for any
    other agent id — even same-team — return 404. The shared shim resolves
    any manifest in the registry, so the middleware below is the sole gate
    that enforces the single-agent-per-sandbox contract.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.requests import Request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("agent_sandbox")

EXIT_MISSING_ENV = 2
EXIT_UNKNOWN_AGENT = 3
EXIT_REGISTRY_LOAD_ERROR = 4

_INVOKE_PATH_PREFIX = "/_agents/"


def _load_sandbox_secrets() -> None:
    """Read ``KEY=VALUE`` pairs from ``SANDBOX_SECRETS_FILE`` into ``os.environ``.

    The provisioner bind-mounts a 0400 file at ``/run/secrets/sandbox-env``
    and sets ``SANDBOX_SECRETS_FILE`` pointing at it — so agent-consumed libs
    (``ollama``, ``shared.postgres``, etc.) keep reading creds from the env
    while the container's startup env (as seen by ``docker inspect`` /
    ``docker exec env``) stays free of them.

    After a successful load we unlink the in-sandbox view so agent code can't
    ``cat /run/secrets/sandbox-env``. The host-side file is cleaned up by the
    lifecycle when the container is torn down.

    No-op when the env marker is unset or the file is missing; keeps unit
    tests and non-sandbox invocations working unchanged.
    """
    path_str = os.environ.get("SANDBOX_SECRETS_FILE")
    if not path_str:
        return
    path = Path(path_str)
    if not path.exists():
        return
    loaded = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        os.environ[key] = value
        loaded += 1
    log.info("Loaded %d sandbox secrets", loaded)
    # Bind-mounted read-only view can't always be unlinked; best-effort.
    with contextlib.suppress(OSError):
        path.unlink()


def _maybe_register_injected_manifest(registry) -> None:
    """Register the provision-time injected manifest into the in-container registry.

    The sandbox boots from its own on-disk registry, which does NOT contain
    dynamically-registered agents (Agent Studio saves / generated team agents).
    The provisioner writes the bound agent's platform-authored manifest to
    ``SANDBOX_AGENT_MANIFEST_FILE`` and bind-mounts it read-only; registering it
    here — before the unknown-agent gate — lets such an agent boot and be invoked
    (the invoke shim resolves from this same ``get_registry()`` singleton).

    Best-effort by design: a missing env var is silently a no-op, a missing file
    logs at info level, and malformed JSON logs a warning — none of these raise.
    The subsequent ``registry.get(agent_id) is None`` gate remains the single
    authority on whether the agent can boot, so a
    static agent (already in the image) is unaffected, and re-registering a static
    id is a harmless idempotent overwrite.
    """
    path_str = os.environ.get("SANDBOX_AGENT_MANIFEST_FILE")
    if not path_str:
        return
    path = Path(path_str)
    if not path.exists():
        log.info("no injected manifest at %s; using on-disk registry only", path_str)
        return
    try:
        from agent_platform.registry.models import AgentManifest

        raw = json.loads(path.read_text(encoding="utf-8"))
        manifest = AgentManifest.model_validate(raw)
        registry.register(manifest)
        log.info("registered injected manifest: agent_id=%s", manifest.id)
    except Exception as exc:  # noqa: BLE001 - best-effort; the None-gate stays authoritative
        log.warning("could not register injected manifest from %s: %s", path_str, exc)


def _build_app() -> FastAPI:
    _load_sandbox_secrets()
    agent_id = os.environ.get("SANDBOX_AGENT_ID")
    if not agent_id:
        log.error("FATAL: SANDBOX_AGENT_ID env var is required")
        sys.exit(EXIT_MISSING_ENV)

    try:
        from agent_platform.registry import get_registry
    except Exception as exc:
        log.exception("FATAL: could not import agent_platform.registry: %s", exc)
        sys.exit(EXIT_REGISTRY_LOAD_ERROR)

    try:
        registry = get_registry()
    except Exception as exc:
        log.exception("FATAL: agent_platform.registry load failed: %s", exc)
        sys.exit(EXIT_REGISTRY_LOAD_ERROR)

    # Register the provision-time injected manifest (if any) so a dynamically
    # registered agent — absent from this image's on-disk registry — can boot.
    _maybe_register_injected_manifest(registry)

    # AgentRegistry.get() returns None for unknown ids; it does not raise.
    manifest = registry.get(agent_id)
    if manifest is None:
        log.error("FATAL: agent_id %r not found in registry", agent_id)
        sys.exit(EXIT_UNKNOWN_AGENT)

    log.info(
        "sandbox starting: agent_id=%s team=%s entrypoint=%s",
        manifest.id,
        manifest.team,
        manifest.source.entrypoint if manifest.source else "<none>",
    )

    app = FastAPI(title=f"agent-sandbox:{manifest.id}")
    bound_agent_id = manifest.id

    @app.middleware("http")
    async def _single_agent_guard(request: Request, call_next):
        """Reject invoke requests for any agent other than the bound one.

        The shared shim resolves any manifest in the registry, so this
        middleware is the sole gate that ensures a sandbox started for
        ``blogging.planner`` can't be tricked into invoking
        ``blogging.writer`` via the same ``/_agents/{id}/invoke`` route.
        """
        path = request.url.path
        if path.startswith(_INVOKE_PATH_PREFIX):
            # Expected shape: /_agents/{agent_id}/invoke[/...]
            remainder = path[len(_INVOKE_PATH_PREFIX) :]
            requested_id = remainder.split("/", 1)[0]
            if requested_id and requested_id != bound_agent_id:
                return JSONResponse(
                    status_code=404,
                    content={
                        "detail": (f"Sandbox is bound to {bound_agent_id!r}; refusing invoke for {requested_id!r}."),
                    },
                )
        return await call_next(request)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "agent_id": bound_agent_id, "team": manifest.team}

    # Mount the shared invoke shim; the middleware above restricts dispatch
    # to the single bound agent.
    #
    # Do not register llm_service.usage_flusher here. Sandboxes are
    # network-isolated from platform Postgres (own khala_sandbox DB, torn
    # down with the stack); usage persistence belongs on the platform
    # side of the invoke boundary, not inside the sandbox.
    from shared.agent_invoke import mount_invoke_shim

    mount_invoke_shim(app)

    return app


def main() -> None:
    app = _build_app()
    uvicorn.run(app, host="0.0.0.0", port=8090, workers=1, log_level="info")


if __name__ == "__main__":
    main()
