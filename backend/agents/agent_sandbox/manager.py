"""
SandboxManager — warm-reuse lifecycle for per-team Docker sandboxes.

State machine per team:

    COLD ──(ensure_warm)──► WARMING ──(health OK)──► WARM
                                │                      │
                                │                      └──(teardown / idle)──► COLD
                                │
                                └──(health fail)─────────► ERROR ──► COLD
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import httpx

from . import compose as compose_mod
from . import state as state_mod
from .config import (
    TEAM_SANDBOX_CONFIGS,
    TeamSandboxConfig,
    boot_timeout_seconds,
    compose_file_path,
    idle_teardown_seconds,
    state_file_path,
)
from .models import SandboxHandle, SandboxState, SandboxStatus

logger = logging.getLogger(__name__)


class UnknownTeamError(ValueError):
    """Raised when the caller asks for a team that has no sandbox configured."""


class SandboxManager:
    """Per-process singleton. Orchestrates docker compose for each team sandbox."""

    def __init__(
        self,
        *,
        compose_file: Path | None = None,
        state_file: Path | None = None,
        configs: dict[str, TeamSandboxConfig] | None = None,
    ) -> None:
        self._compose_file = compose_file or compose_file_path()
        self._state_file = state_file or state_file_path()
        self._configs = configs or TEAM_SANDBOX_CONFIGS
        self._state: dict[str, SandboxState] = state_mod.load(self._state_file)
        self._locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def known_teams(self) -> list[str]:
        return sorted(self._configs)

    def get_config(self, team: str) -> TeamSandboxConfig:
        try:
            return self._configs[team]
        except KeyError as exc:
            raise UnknownTeamError(f"No sandbox configured for team {team!r}") from exc

    async def ensure_warm(self, team: str) -> SandboxHandle:
        """Idempotently bring the sandbox for ``team`` to WARM. Returns a handle."""
        cfg = self.get_config(team)
        lock = self._locks.setdefault(team, asyncio.Lock())
        async with lock:
            existing = self._state.get(team)
            if existing and existing.status == SandboxStatus.WARM:
                # Already warm; check it's actually still running before trusting state.
                if await compose_mod.is_running(self._compose_file, cfg.service_name):
                    existing.last_used_at = state_mod.now()
                    self._persist()
                    return self._handle(cfg, existing)
                logger.info("Sandbox %s marked WARM but not actually running; re-warming", team)

            logger.info("Warming sandbox for team %s (service %s)", team, cfg.service_name)
            st = state_mod.new_state(team, cfg.service_name, cfg.container_name, cfg.host_port)
            self._state[team] = st
            self._persist()
            try:
                await compose_mod.up_detached(self._compose_file, cfg.service_name)
                await self._wait_healthy(cfg)
                st.status = SandboxStatus.WARM
                st.error = None
                st.last_used_at = state_mod.now()
                self._persist()
                return self._handle(cfg, st)
            except Exception as exc:
                logger.exception("Sandbox warm failed for %s", team)
                st.status = SandboxStatus.ERROR
                st.error = str(exc)
                self._persist()
                return self._handle(cfg, st)

    async def status(self, team: str) -> SandboxHandle:
        cfg = self.get_config(team)
        st = self._state.get(team)
        if st is None:
            return SandboxHandle(
                team=team,
                status=SandboxStatus.COLD,
                url=None,
                service_name=cfg.service_name,
                container_name=cfg.container_name,
                host_port=cfg.host_port,
            )
        # Reconcile: if we think it's WARM but the container isn't running, correct.
        if st.status == SandboxStatus.WARM:
            if not await compose_mod.is_running(self._compose_file, cfg.service_name):
                st.status = SandboxStatus.COLD
                self._persist()
        return self._handle(cfg, st)

    async def teardown(self, team: str) -> None:
        cfg = self.get_config(team)
        lock = self._locks.setdefault(team, asyncio.Lock())
        async with lock:
            logger.info("Tearing down sandbox for team %s", team)
            try:
                await compose_mod.stop_and_remove(self._compose_file, cfg.service_name)
            except compose_mod.ComposeError as exc:
                logger.warning("teardown for %s reported non-zero: %s", team, exc)
            self._state.pop(team, None)
            self._persist()

    async def list_warm(self) -> list[SandboxHandle]:
        out: list[SandboxHandle] = []
        for team, st in list(self._state.items()):
            if st.status != SandboxStatus.WARM:
                continue
            cfg = self._configs.get(team)
            if cfg is None:
                continue
            out.append(self._handle(cfg, st))
        return out

    async def note_activity(self, team: str) -> None:
        """Bump last_used_at for ``team``. Called after a successful invocation."""
        st = self._state.get(team)
        if st is None:
            return
        st.last_used_at = state_mod.now()
        self._persist()

    # ------------------------------------------------------------------
    # Idle reaper
    # ------------------------------------------------------------------

    async def run_idle_reaper(self, *, interval_s: int = 60) -> None:
        """Background loop: tear down sandboxes idle for more than SANDBOX_IDLE_TEARDOWN_MINUTES."""
        threshold = idle_teardown_seconds()
        logger.info(
            "Sandbox idle reaper started (threshold %ds, check every %ds)", threshold, interval_s
        )
        while True:
            try:
                await asyncio.sleep(interval_s)
                now = state_mod.now()
                for team, st in list(self._state.items()):
                    if st.status != SandboxStatus.WARM:
                        continue
                    idle = (now - st.last_used_at).total_seconds()
                    if idle > threshold:
                        logger.info("Reaping idle sandbox %s (idle=%.0fs)", team, idle)
                        await self.teardown(team)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("idle reaper iteration failed; continuing")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _wait_healthy(self, cfg: TeamSandboxConfig) -> None:
        deadline = boot_timeout_seconds()
        url = f"{cfg.url}{cfg.health_path}"
        start = asyncio.get_event_loop().time()
        backoff = 1.0
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
            while True:
                elapsed = asyncio.get_event_loop().time() - start
                if elapsed > deadline:
                    raise RuntimeError(
                        f"Sandbox {cfg.service_name} did not report healthy within {deadline}s"
                    )
                try:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 5.0)

    def _handle(self, cfg: TeamSandboxConfig, st: SandboxState) -> SandboxHandle:
        idle = None
        if st.last_used_at is not None:
            idle = int((datetime.now(timezone.utc) - st.last_used_at).total_seconds())
        return SandboxHandle(
            team=cfg.team,
            status=st.status,
            url=cfg.url if st.status == SandboxStatus.WARM else None,
            service_name=cfg.service_name,
            container_name=cfg.container_name,
            host_port=cfg.host_port,
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


@lru_cache(maxsize=1)
def get_manager() -> SandboxManager:
    return SandboxManager()
