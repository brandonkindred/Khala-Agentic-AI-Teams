"""Per-agent sandbox lifecycle owner (issue #264, Phase 2).

State machine per ``agent_id``::

    COLD ──(acquire)──► WARMING ──(health OK)──► WARM
                           │                       │
                           │                       └──(teardown / idle)──► COLD
                           │
                           └──(run/health fail)────► ERROR ──► COLD

Provisions the unified ``khala-agent-sandbox`` image from Phase 1 (#263) one
container per specialist agent. Blocks Phases 3–5. Replaces — in parallel,
without deleting — the per-team state machine in ``backend/agents/agent_sandbox``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

import httpx

from . import provisioner as provisioner_mod
from . import state as state_mod
from .state import (
    SandboxHandle,
    SandboxState,
    SandboxStatus,
    boot_timeout_seconds,
    idle_teardown_seconds,
    now,
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

    Mirrors the public shape of ``agent_sandbox.manager.SandboxManager`` but
    rekeyed by ``agent_id`` and talking to ``docker run`` / ``docker inspect``
    directly (Phase 2), not ``docker compose`` (legacy per-team path).
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
                    return self._handle(existing)
                logger.info(
                    "Sandbox for %s marked WARM but container %s is gone; re-provisioning",
                    agent_id,
                    existing.container_id,
                )

            container_name = provisioner_mod.container_name_for(agent_id)
            # Sweep away any zombie container from a prior run that died before
            # we could update state. Safe: `stop_container` is idempotent.
            await self._best_effort_reap(container_name)

            logger.info("Provisioning sandbox for %s (container %s)", agent_id, container_name)
            st = state_mod.new_state(agent_id=agent_id, team=team, container_name=container_name)
            self._state[agent_id] = st
            self._persist()

            try:
                container_id = await provisioner_mod.run_container(
                    agent_id=agent_id, container_name=container_name
                )
                host_port = await provisioner_mod.inspect_host_port(container_id)
                st.container_id = container_id
                st.host_port = host_port
                self._persist()
                await self._wait_healthy(host_port)
                st.status = SandboxStatus.WARM
                st.error = None
                st.last_used_at = now()
                self._persist()
                return self._handle(st)
            except Exception as exc:
                logger.exception("Sandbox provisioning failed for %s", agent_id)
                st.status = SandboxStatus.ERROR
                st.error = str(exc)
                self._persist()
                return self._handle(st)

    async def release(self, agent_id: str) -> None:
        """Mark the sandbox as idle-eligible. Does not tear down — the reaper decides."""
        st = self._state.get(agent_id)
        if st is None:
            return
        st.last_used_at = now()
        self._persist()

    async def teardown(self, agent_id: str) -> None:
        """Explicitly stop the sandbox for ``agent_id`` and evict from state."""
        lock = self._locks.setdefault(agent_id, asyncio.Lock())
        async with lock:
            st = self._state.get(agent_id)
            if st is None:
                return
            logger.info("Tearing down sandbox for %s", agent_id)
            if st.container_id:
                try:
                    await provisioner_mod.stop_container(st.container_id)
                except provisioner_mod.DockerError as exc:
                    logger.warning("teardown for %s reported non-zero: %s", agent_id, exc)
            self._state.pop(agent_id, None)
            self._persist()

    async def list_active(self) -> list[SandboxHandle]:
        """Return a handle for every sandbox currently tracked in state."""
        return [self._handle(st) for st in list(self._state.values())]

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
            if idle > threshold:
                logger.info("Reaping idle sandbox %s (idle=%.0fs)", agent_id, idle)
                await self.teardown(agent_id)
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

    async def _best_effort_reap(self, container_name: str) -> None:
        """If a container with ``container_name`` exists from a prior run, remove it."""
        try:
            await provisioner_mod.stop_container(container_name)
        except provisioner_mod.DockerError as exc:
            logger.debug(
                "best-effort reap of %s failed (likely already gone): %s", container_name, exc
            )

    def _handle(self, st: SandboxState) -> SandboxHandle:
        idle = None
        if st.last_used_at is not None:
            idle = int((datetime.now(timezone.utc) - st.last_used_at).total_seconds())
        url = None
        if st.status == SandboxStatus.WARM and st.host_port is not None:
            url = f"http://127.0.0.1:{st.host_port}"
        return SandboxHandle(
            agent_id=st.agent_id,
            team=st.team,
            status=st.status,
            url=url,
            container_name=st.container_name,
            container_id=st.container_id,
            host_port=st.host_port,
            created_at=st.created_at,
            last_used_at=st.last_used_at,
            idle_seconds=idle,
            error=st.error,
        )

    def _persist(self) -> None:
        try:
            state_mod.save(self._state_file, self._state)
        except OSError as exc:
            logger.warning("Could not persist sandbox state: %s", exc)
