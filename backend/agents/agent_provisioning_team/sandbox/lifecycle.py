"""Per-agent sandbox lifecycle owner (issue #264, Phase 2).

State machine per ``agent_id``:

.. mermaid::

    stateDiagram-v2
        [*] --> COLD
        COLD --> WARMING: acquire
        WARMING --> WARM: health OK
        WARMING --> ERROR: run / health fail
        WARM --> COLD: teardown / idle reap
        ERROR --> COLD: teardown
        COLD --> [*]

Provisions the unified ``khala-agent-sandbox`` image from Phase 1 (#263) one
container per specialist agent.
"""

from __future__ import annotations

import asyncio
import logging
import time
from functools import lru_cache
from pathlib import Path

import httpx

from . import provisioner as provisioner_mod
from . import state as state_mod
from .state import (
    COLD_START_LOG_PREFIX,
    SandboxHandle,
    SandboxState,
    SandboxStatus,
    boot_timeout_seconds,
    idle_teardown_seconds,
    now,
    sandbox_image,
    state_file_path,
)

logger = logging.getLogger(__name__)


class UnknownAgentError(ValueError):
    """Raised when the requested ``agent_id`` has no manifest in the registry."""


def _resolve_team(agent_id: str) -> str:
    """Look up the agent's team via :mod:`agent_registry`.

    Wrapped so tests can patch it without importing the whole registry.
    """
    from agent_registry import get_registry

    manifest = get_registry().get(agent_id)
    if manifest is None:
        raise UnknownAgentError(f"No agent manifest for {agent_id!r}")
    return manifest.team


class Lifecycle:
    """Per-process owner of agent-keyed sandboxes.

    Keyed by ``agent_id``; talks to ``docker run`` / ``docker inspect``
    directly.
    """

    def __init__(self, *, state_file: Path | None = None) -> None:
        self._state_file = state_file or state_file_path()
        self._state: dict[str, SandboxState] = state_mod.load(self._state_file)
        self._locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def acquire(self, agent_id: str) -> SandboxHandle:
        """Idempotently bring the sandbox for ``agent_id`` to WARM.

        Raises :class:`UnknownAgentError` if the registry has no entry for
        ``agent_id``.
        """
        team = _resolve_team(agent_id)
        lock = self._locks.setdefault(agent_id, asyncio.Lock())
        async with lock:
            existing = self._state.get(agent_id)
            if existing and existing.status == SandboxStatus.WARM and existing.container_id:
                if await provisioner_mod.is_running(existing.container_id):
                    existing.last_used_at = now()
                    self._persist()
                    return SandboxHandle.from_state(existing)
                logger.info(
                    "Sandbox for %s marked WARM but container %s is gone; re-provisioning",
                    agent_id,
                    existing.container_id,
                )

            container_name = provisioner_mod.container_name_for(agent_id)
            # Sweep any zombie container from a prior run. `docker rm -f` is
            # idempotent against missing containers; timeouts are surfaced.
            await provisioner_mod.stop_container(container_name)
            # And any stale secrets file the previous sandbox may have left
            # behind before the host process died — run_container will write
            # a fresh one.
            provisioner_mod.cleanup_secrets_file(container_name)

            logger.info("Provisioning sandbox for %s (container %s)", agent_id, container_name)
            st = state_mod.new_state(agent_id=agent_id, team=team, container_name=container_name)
            self._state[agent_id] = st

            cold_start = time.perf_counter()
            try:
                container_id = await provisioner_mod.run_container(
                    agent_id=agent_id, container_name=container_name, team=team
                )
                host_port = await provisioner_mod.inspect_host_port(container_id)
                st.container_id = container_id
                st.host_port = host_port
                await self._wait_healthy(host_port)
                st.status = SandboxStatus.WARM
                st.last_used_at = now()
                self._persist()
                boot_ms = int((time.perf_counter() - cold_start) * 1000)
                logger.info(
                    "%s agent_id=%s team=%s image=%s boot_ms=%d",
                    COLD_START_LOG_PREFIX,
                    agent_id,
                    team,
                    sandbox_image(),
                    boot_ms,
                )
                return SandboxHandle.from_state(st, boot_ms=boot_ms)
            except Exception as exc:
                logger.exception("Sandbox provisioning failed for %s", agent_id)
                st.status = SandboxStatus.ERROR
                st.error = str(exc)
                self._persist()
                return SandboxHandle.from_state(st)

    async def status(self, agent_id: str) -> SandboxHandle:
        """Return a handle for ``agent_id`` (COLD if we've never seen it).

        Reconciles against Docker: if we believe the container is WARM but
        ``docker inspect`` reports it gone, flip the state to COLD so the
        caller sees reality.
        """
        st = self._state.get(agent_id)
        if st is None:
            team = _resolve_team(agent_id)
            return SandboxHandle(
                agent_id=agent_id,
                team=team,
                status=SandboxStatus.COLD,
                container_name=provisioner_mod.container_name_for(agent_id),
            )
        if (
            st.status == SandboxStatus.WARM
            and st.container_id
            and not await provisioner_mod.is_running(st.container_id)
        ):
            st.status = SandboxStatus.COLD
            self._persist()
        return SandboxHandle.from_state(st)

    async def teardown(self, agent_id: str) -> None:
        """Explicitly stop the sandbox for ``agent_id`` and evict from state.

        State is only evicted after Docker confirms the container is gone:
        ``stop_container`` raises :class:`DockerError` for real failures
        (e.g. daemon unreachable), which we propagate so the caller (or the
        reaper's next tick) can retry against a sandbox that is still alive.
        """
        lock = self._locks.setdefault(agent_id, asyncio.Lock())
        async with lock:
            st = self._state.get(agent_id)
            if st is None:
                return
            logger.info("Tearing down sandbox for %s", agent_id)
            if st.container_id:
                await provisioner_mod.stop_container(st.container_id)
            # Secrets file is keyed by container_name; clean it up after the
            # container is confirmed gone so we don't leave 0400 files on the
            # host when an agent never runs again.
            provisioner_mod.cleanup_secrets_file(st.container_name)
            self._state.pop(agent_id, None)
            self._persist()

    async def list_active(self) -> list[SandboxHandle]:
        """Return a handle for every sandbox currently tracked in state."""
        return [SandboxHandle.from_state(st) for st in list(self._state.values())]

    async def note_activity(self, agent_id: str) -> None:
        """Bump ``last_used_at`` for ``agent_id``. Called after a successful invoke."""
        st = self._state.get(agent_id)
        if st is None:
            return
        st.last_used_at = now()
        self._persist()

    # ------------------------------------------------------------------
    # Idle reaper
    # ------------------------------------------------------------------

    async def run_idle_reaper(self, *, interval_s: int = 60) -> None:
        """Background loop: tear down sandboxes idle for more than the threshold.

        Threshold is :func:`state.idle_teardown_seconds` (default 5 min, env
        ``AGENT_PROVISIONING_SANDBOX_IDLE_MINUTES``). Loop is cancellable.
        """
        threshold = idle_teardown_seconds()
        logger.info(
            "Agent sandbox idle reaper started (threshold %ds, check every %ds)",
            threshold,
            interval_s,
        )
        while True:
            try:
                await asyncio.sleep(interval_s)
                await self.reap_once(threshold=threshold)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("idle reaper iteration failed; continuing")

    async def reap_once(self, *, threshold: int) -> list[str]:
        """Tear down every WARM sandbox idle longer than ``threshold`` seconds.

        Returns the list of torn-down ``agent_id``s so callers (and tests) can
        observe the effect without waiting a full reap interval.
        """
        torn_down: list[str] = []
        current = now()
        for agent_id, st in list(self._state.items()):
            if st.status != SandboxStatus.WARM:
                continue
            idle = (current - st.last_used_at).total_seconds()
            if idle <= threshold:
                continue
            logger.info("Reaping idle sandbox %s (idle=%.0fs)", agent_id, idle)
            try:
                await self.teardown(agent_id)
            except provisioner_mod.DockerError:
                logger.exception("Teardown failed for %s; will retry next tick", agent_id)
                continue
            torn_down.append(agent_id)
        return torn_down

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _wait_healthy(self, host_port: int) -> None:
        deadline = boot_timeout_seconds()
        url = f"http://127.0.0.1:{host_port}/health"
        start = asyncio.get_event_loop().time()
        backoff = 1.0
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
            while True:
                elapsed = asyncio.get_event_loop().time() - start
                if elapsed > deadline:
                    raise RuntimeError(
                        f"Sandbox on port {host_port} did not report healthy within {deadline}s"
                    )
                try:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 5.0)

    def _persist(self) -> None:
        try:
            state_mod.save(self._state_file, self._state)
        except OSError as exc:
            logger.warning("Could not persist sandbox state: %s", exc)


# Module-level free-function wrappers over a process-wide singleton — Phase 3
# (#265) wires the unified API through these so routes don't construct a
# Lifecycle at every call site. Tests swap via ``get_lifecycle.cache_clear()``
# plus a temporary module-attribute override.


@lru_cache(maxsize=1)
def get_lifecycle() -> Lifecycle:
    return Lifecycle()


async def acquire(agent_id: str) -> SandboxHandle:
    return await get_lifecycle().acquire(agent_id)


async def status(agent_id: str) -> SandboxHandle:
    return await get_lifecycle().status(agent_id)


async def teardown(agent_id: str) -> None:
    await get_lifecycle().teardown(agent_id)


async def list_active() -> list[SandboxHandle]:
    return await get_lifecycle().list_active()


async def note_activity(agent_id: str) -> None:
    await get_lifecycle().note_activity(agent_id)


async def run_idle_reaper(*, interval_s: int = 60) -> None:
    await get_lifecycle().run_idle_reaper(interval_s=interval_s)
