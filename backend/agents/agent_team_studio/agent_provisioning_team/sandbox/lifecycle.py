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
import json
import logging
import math
import os
import shutil
import threading
import time
from collections import Counter, deque
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import httpx

from . import provisioner as provisioner_mod
from . import state as state_mod
from .state import (
    COLD_START_LOG_PREFIX,
    AgeStats,
    BootMsStats,
    ReaperStats,
    SandboxHandle,
    SandboxMetrics,
    SandboxState,
    SandboxStatus,
    boot_timeout_seconds,
    idle_teardown_seconds,
    now,
    sandbox_image,
    state_file_path,
)

# Cap on how many recent boot_ms observations we keep in memory. 500 samples
# at 4 bytes each is negligible; large enough for stable p95 under churn.
_BOOT_MS_WINDOW = 500

logger = logging.getLogger(__name__)


class UnknownAgentError(ValueError):
    """Raised when the requested ``agent_id`` has no manifest in the registry."""


async def _resolve_team(agent_id: str) -> str:
    """Look up the agent's team via :mod:`agent_platform.registry`.

    Wrapped so tests can patch it without importing the whole registry.

    Preconditions:
        * ``agent_id`` is a string.
    Postconditions:
        * Returns the resolved manifest's ``team``, or raises
          :class:`UnknownAgentError` if unresolvable. Both ``get_registry()``
          and the ``.get(agent_id)`` lookup run inside the ``asyncio.to_thread``
          worker — not just the lookup — so a cold-cache first call (which
          performs ``AgentRegistry.load()``'s full manifest directory scan +
          YAML parse across every team, not a cheap singleton fetch) never runs
          on this coroutine's event loop. ``get_registry`` is
          ``functools.lru_cache``-wrapped, whose internal lock already
          serializes concurrent cold-cache calls, so no additional locking is
          needed here.
    """

    def _lookup():
        from agent_platform.registry import get_registry

        return get_registry().get(agent_id)

    manifest = await asyncio.to_thread(_lookup)
    if manifest is None:
        raise UnknownAgentError(f"No agent manifest for {agent_id!r}")
    return manifest.team


class DockerUnavailableError(RuntimeError):
    """Raised when the ``docker`` CLI is not installed or the daemon is unreachable."""


class SandboxAcquireFailedError(RuntimeError):
    """Raised by the Temporal ``sandbox_acquire_activity`` when a transient
    provisioning failure survives every ``SANDBOX_ACQUIRE_RETRY_POLICY``
    attempt.

    ``Lifecycle.acquire()`` itself never raises this — a transient failure
    inside its provisioning try block is caught internally and returned as a
    non-raising ERROR-status :class:`~agent_team_studio.agent_provisioning_team.sandbox.state.SandboxHandle`
    (see the ``except Exception`` block below), so direct/thread-mode callers
    always get a handle back. This type exists purely as the Temporal-side
    "retries exhausted" marker: the activity raises it so Temporal's retry
    policy can retry the transient failure, and once retries are truly
    exhausted, ``sandbox_dispatch._reraise_sandbox_error`` translates it back
    to this exact type so ``POST /warm`` can map it to a clean 503 instead of
    an opaque ``WorkflowFailureError``.
    """


def _has_non_default_docker_context() -> bool:
    """Return True when the Docker config selects a non-default context.

    Respects ``DOCKER_CONFIG`` (falls back to ``~/.docker``).
    """
    config_dir = Path(os.environ.get("DOCKER_CONFIG") or (Path.home() / ".docker"))
    config_path = config_dir / "config.json"
    try:
        data = json.loads(config_path.read_text())
        ctx = data.get("currentContext", "")
        return bool(ctx and ctx != "default")
    except Exception:  # noqa: BLE001
        return False


def _check_docker_available() -> None:
    """Fail fast with a clear message when docker is not usable.

    Preconditions: none.
    Postconditions: returns normally only when the ``docker`` binary is on
    PATH and a Docker endpoint is reachable (DOCKER_HOST, DOCKER_CONTEXT,
    a persisted active context in ~/.docker/config.json, or the default
    /var/run/docker.sock).
    """
    if shutil.which("docker") is None:
        raise DockerUnavailableError(
            "Sandbox provisioning requires the 'docker' CLI, but it is not installed or not on PATH. "
            "Install Docker or run the unified API on a host with Docker access."
        )
    docker_context = os.environ.get("DOCKER_CONTEXT", "")
    if os.environ.get("DOCKER_HOST") or (docker_context and docker_context != "default"):
        return
    if _has_non_default_docker_context():
        return
    docker_sock = Path("/var/run/docker.sock")
    if not docker_sock.exists():
        raise DockerUnavailableError(
            "Sandbox provisioning requires the Docker daemon, but /var/run/docker.sock is missing "
            "and neither DOCKER_HOST nor DOCKER_CONTEXT is set. "
            "Start the Docker daemon, set DOCKER_HOST, or bind-mount the socket into this container."
        )


class Lifecycle:
    """Per-process owner of agent-keyed sandboxes.

    Keyed by ``agent_id``; talks to ``docker run`` / ``docker inspect``
    directly.
    """

    def __init__(self, *, state_file: Path | None = None) -> None:
        self._state_file = state_file or state_file_path()
        self._state: dict[str, SandboxState] = state_mod.load(self._state_file)
        self._locks: dict[str, asyncio.Lock] = {}
        # Thread-safety invariant: every read/write of ``_state`` and the
        # observability counters below is serialized by this lock. Needed once
        # acquire/teardown/reap run on the Temporal worker's event loop (a
        # different OS thread) while status/note_activity/list_active/metrics
        # stay on the API loop. The per-agent ``asyncio.Lock``s above guard the
        # long docker critical sections; this lock only ever wraps brief
        # synchronous reads/writes and is NEVER held across an ``await`` —
        # holding a ``threading.RLock`` across an ``await`` would let unrelated
        # coroutines on the same OS thread "reenter" it (RLock reentrancy is
        # thread-identity-based, not coroutine-based) and block the *entire*
        # event loop for as long as the lock is held elsewhere. ``_persist()``
        # snapshots under this lock and does its actual disk write outside it,
        # via ``asyncio.to_thread``. RLock so a locked block can call another
        # method that also takes the lock without deadlocking itself.
        self._state_lock = threading.RLock()
        # Observability counters (issue #302). In-process only — reset on restart.
        self._boot_ms_samples: deque[int] = deque(maxlen=_BOOT_MS_WINDOW)
        self._torn_down_total: int = 0
        self._torn_down_last_tick: int = 0
        self._reaper_last_tick_at: datetime | None = None
        self._reaper_interval_s: int | None = None
        # Monotonic sequence guarding _persist()'s write against out-of-order
        # completion: since each call's disk write runs unlocked (see
        # _persist()'s docstring), a slower write of an older snapshot could
        # otherwise land after a faster write of a newer one and revert
        # state_file to stale content. Guarded by _state_lock.
        self._persist_seq: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def acquire(self, agent_id: str) -> SandboxHandle:
        """Idempotently bring the sandbox for ``agent_id`` to WARM.

        Raises :class:`UnknownAgentError` if the registry has no entry for
        ``agent_id``.
        """
        team = await _resolve_team(agent_id)
        lock = self._locks.setdefault(agent_id, asyncio.Lock())
        async with lock:
            with self._state_lock:
                existing = self._state.get(agent_id)
            if existing and existing.status == SandboxStatus.WARM and existing.container_id:
                try:
                    still_running = await provisioner_mod.is_running(existing.container_id)
                except Exception:
                    logger.warning(
                        "Could not check container status for %s; returning cached warm handle",
                        agent_id,
                        exc_info=True,
                    )
                    with self._state_lock:
                        existing.last_used_at = now()
                    await self._persist()
                    return SandboxHandle.from_state(existing)
                if still_running:
                    with self._state_lock:
                        existing.last_used_at = now()
                    await self._persist()
                    return SandboxHandle.from_state(existing)
                logger.info(
                    "Sandbox for %s marked WARM but container %s is gone; re-provisioning",
                    agent_id,
                    existing.container_id,
                )

            _check_docker_available()

            container_name = provisioner_mod.container_name_for(agent_id)
            try:
                await provisioner_mod.stop_container(container_name)
            except provisioner_mod.DockerError:
                raise
            except Exception:
                logger.warning(
                    "Zombie cleanup failed for %s; continuing to provision",
                    container_name,
                    exc_info=True,
                )
            provisioner_mod.cleanup_secrets_file(container_name)

            logger.info("Provisioning sandbox for %s (container %s)", agent_id, container_name)
            st = state_mod.new_state(agent_id=agent_id, team=team, container_name=container_name)
            with self._state_lock:
                self._state[agent_id] = st

            cold_start = time.perf_counter()
            try:
                container_id = await provisioner_mod.run_container(
                    agent_id=agent_id, container_name=container_name, team=team
                )
                host_port = await provisioner_mod.inspect_host_port(container_id)
                with self._state_lock:
                    st.container_id = container_id
                    st.host_port = host_port
                await self._wait_healthy(host_port)
                with self._state_lock:
                    st.status = SandboxStatus.WARM
                    st.last_used_at = now()
                await self._persist()
                boot_ms = int((time.perf_counter() - cold_start) * 1000)
                logger.info(
                    "%s agent_id=%s team=%s image=%s boot_ms=%d",
                    COLD_START_LOG_PREFIX,
                    agent_id,
                    team,
                    sandbox_image(),
                    boot_ms,
                )
                with self._state_lock:
                    self._boot_ms_samples.append(boot_ms)
                return SandboxHandle.from_state(st, boot_ms=boot_ms)
            except Exception as exc:
                logger.exception("Sandbox provisioning failed for %s", agent_id)
                with self._state_lock:
                    st.status = SandboxStatus.ERROR
                    st.error = str(exc)
                await self._persist()
                return SandboxHandle.from_state(st)

    async def status(self, agent_id: str) -> SandboxHandle:
        """Return a handle for ``agent_id`` (COLD if we've never seen it).

        Reconciles against Docker: if we believe the container is WARM but
        ``docker inspect`` reports it gone, flip the state to COLD so the
        caller sees reality.

        Raises :class:`UnknownAgentError` (propagated from :func:`_resolve_team`)
        if ``agent_id`` has no entry in the registry and we have no prior tracked
        state for it — mirrors :meth:`acquire`'s contract.
        """
        with self._state_lock:
            st = self._state.get(agent_id)
        if st is None:
            team = await _resolve_team(agent_id)
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
            with self._state_lock:
                st.status = SandboxStatus.COLD
            await self._persist()
        # Snapshot immediately before the multi-field read below (see
        # list_active()'s docstring) — ``st`` is still the live object tracked
        # in ``self._state``, which a concurrent acquire()/teardown() could be
        # mutating field-by-field.
        with self._state_lock:
            snapshot = st.model_copy()
        return SandboxHandle.from_state(snapshot)

    async def teardown(self, agent_id: str) -> None:
        """Explicitly stop the sandbox for ``agent_id`` and evict from state.

        State is only evicted after Docker confirms the container is gone:
        ``stop_container`` raises :class:`DockerError` for real failures
        (e.g. daemon unreachable), which we propagate so the caller (or the
        reaper's next tick) can retry against a sandbox that is still alive.
        """
        lock = self._locks.setdefault(agent_id, asyncio.Lock())
        async with lock:
            with self._state_lock:
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
            with self._state_lock:
                self._state.pop(agent_id, None)
            await self._persist()

    async def list_active(self) -> list[SandboxHandle]:
        """Return a handle for every sandbox currently tracked in state."""
        with self._state_lock:
            # model_copy() snapshots each object's *field values*, not just the
            # list of references — SandboxHandle.from_state() below then reads
            # independent copies, safe from a concurrent acquire()/teardown()
            # mutating the live SandboxState objects field-by-field after this
            # lock releases.
            snapshot = [st.model_copy() for st in self._state.values()]
        return [SandboxHandle.from_state(st) for st in snapshot]

    async def note_activity(self, agent_id: str) -> None:
        """Bump ``last_used_at`` for ``agent_id``. Called after a successful invoke."""
        with self._state_lock:
            st = self._state.get(agent_id)
            if st is None:
                return
            st.last_used_at = now()
        await self._persist()

    async def metrics(self) -> SandboxMetrics:
        """Return a live snapshot of the sandbox pool (issue #302).

        All counters are in-process and reset when the unified API restarts;
        historical per-invocation data lives in ``agent_console_runs``.
        """
        with self._state_lock:
            # model_copy() so by_status/ages below read independent copies,
            # not live objects a concurrent acquire()/teardown() could still
            # be mutating (see list_active()'s docstring).
            snapshot = [st.model_copy() for st in self._state.values()]
            boot_samples = list(self._boot_ms_samples)
            reaper_last_tick_at = self._reaper_last_tick_at
            reaper_interval_s = self._reaper_interval_s
            torn_down_total = self._torn_down_total
            torn_down_last_tick = self._torn_down_last_tick
        current = now()

        by_team: Counter[str] = Counter(st.team for st in snapshot)
        by_status: Counter[str] = Counter(st.status.value for st in snapshot)

        ages = [int((current - st.created_at).total_seconds()) for st in snapshot]

        return SandboxMetrics(
            resident=len(snapshot),
            by_team=dict(by_team),
            by_status=dict(by_status),
            ages_seconds=_age_stats(ages),
            reaper=ReaperStats(
                last_tick_at=reaper_last_tick_at,
                interval_s=reaper_interval_s,
                threshold_s=idle_teardown_seconds(),
                torn_down_total=torn_down_total,
                torn_down_last_tick=torn_down_last_tick,
            ),
            boot_ms=_boot_ms_stats(boot_samples),
        )

    # ------------------------------------------------------------------
    # Idle reaper
    # ------------------------------------------------------------------

    async def run_idle_reaper(self, *, interval_s: int = 60) -> None:
        """Background loop: tear down sandboxes idle for more than the threshold.

        Threshold is :func:`state.idle_teardown_seconds` (default 5 min, env
        ``AGENT_PROVISIONING_SANDBOX_IDLE_MINUTES``). Loop is cancellable.
        """
        threshold = idle_teardown_seconds()
        self._reaper_interval_s = interval_s
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
        with self._state_lock:
            items = list(self._state.items())
        for agent_id, st in items:
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
        # Stamp the tick even when nothing was torn down — operators need to
        # see the reaper is alive, not just that it found work.
        with self._state_lock:
            self._reaper_last_tick_at = current
            self._torn_down_last_tick = len(torn_down)
            self._torn_down_total += len(torn_down)
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

    async def _persist(self) -> None:
        """Snapshot ``_state`` under the lock, then persist off the event loop.

        Preconditions:
            * Callers must NOT already hold ``self._state_lock`` (this method
              acquires it itself, briefly, and releases it before the ``await``).
              Enforced by an assertion below: ``RLock._is_owned()`` is a private
              CPython API, but it's the only way to catch this violation, and a
              silent reentrant acquire here would hide the exact hazard this
              precondition exists to prevent (see Invariants).
        Postconditions:
            * ``self._state_file`` reflects the ``_state`` snapshot taken at
              call time, or a newer one if a concurrent ``_persist()`` call was
              also in flight (self-healing either way — the next successful
              call always reflects then-current state). Never raises; any
              exception from the threaded write (an ``OSError`` from the
              filesystem, or e.g. a serialization error inside
              ``state_mod.save``) is logged and swallowed, matching the prior
              synchronous behavior — a checkpoint write is best-effort and
              must never surface as an unhandled task exception.

        Invariants:
            * The lock is held only for the synchronous per-agent
              ``model_copy()`` snapshot, never across the ``await``. A
              ``threading.RLock``'s reentrancy is thread-identity-based, not
              coroutine-based, so
              holding it across an ``await`` would let an unrelated coroutine
              resumed on the same OS thread silently "reenter" it, and would
              block that thread's *entire* event loop — not just this
              coroutine — for as long as the disk write takes. Running the
              write via ``asyncio.to_thread`` keeps it off this event loop
              entirely.
            * Two ``_persist()`` calls can be concurrently in flight (one on
              the Temporal worker loop, one on the API loop), and nothing
              orders their unlocked disk writes relative to each other — a
              slower write of an *older* snapshot could otherwise land after a
              faster write of a *newer* one and revert ``state_file`` to stale
              content. ``self._persist_seq`` (bumped when each call snapshots,
              re-checked immediately before the write) closes most of that
              window: once a newer call has been requested, an older call's
              write is skipped rather than risking a stale overwrite — the
              newer call's own write already reflects state at least as fresh.
              This narrows, rather than eliminates, the race (a still-newer
              call could itself start after this check passes); a fully
              airtight guarantee would need a single background writer, which
              is disproportionate for this best-effort checkpoint file — state
              is already reconciled against ``docker inspect`` on next use.
        """
        assert not self._state_lock._is_owned(), (
            "_persist() must not be called while holding _state_lock"
        )  # noqa: SLF001
        with self._state_lock:
            self._persist_seq += 1
            my_seq = self._persist_seq
            # model_copy() each value HERE, while still holding the lock that
            # guards every field mutation (acquire()/teardown() take it around
            # each st.field = ... assignment) — not later, unlocked, inside
            # state_mod.save() on the background thread. Copying after the
            # lock releases would let a field write from an in-flight
            # acquire()/teardown() for the same agent_id land between this
            # snapshot and the eventual model_copy(), producing a torn
            # (partially-old, partially-new) checkpoint despite the lock
            # discipline. Iterating self._state.items() directly (no need for
            # a list() wrapper first) is safe here specifically because every
            # dict-resizing mutation also takes this same lock, so nothing can
            # insert/pop while we hold it.
            snapshot = {agent_id: st.model_copy() for agent_id, st in self._state.items()}

        def _write_if_still_latest() -> None:
            with self._state_lock:
                if my_seq != self._persist_seq:
                    return
            state_mod.save(self._state_file, snapshot)

        try:
            await asyncio.to_thread(_write_if_still_latest)
        except Exception as exc:  # noqa: BLE001
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


async def metrics() -> SandboxMetrics:
    return await get_lifecycle().metrics()


# ----------------------------------------------------------------------
# Percentile helpers (shared between ages_seconds and boot_ms aggregations)
# ----------------------------------------------------------------------


def _percentile(sorted_values: list[int], pct: float) -> int:
    """Nearest-rank percentile on a pre-sorted list. Empty → 0.

    Uses ``ceil(pct/100 * n)`` (the textbook nearest-rank definition) so high
    percentiles don't underestimate for odd sample counts — e.g. n=11, p95
    must land on index 10, not 9.
    """
    if not sorted_values:
        return 0
    idx = max(0, min(len(sorted_values) - 1, math.ceil(pct / 100 * len(sorted_values)) - 1))
    return sorted_values[idx]


def _age_stats(ages: list[int]) -> AgeStats:
    if not ages:
        return AgeStats()
    ordered = sorted(ages)
    return AgeStats(
        min=ordered[0],
        p50=_percentile(ordered, 50),
        p95=_percentile(ordered, 95),
        max=ordered[-1],
    )


def _boot_ms_stats(samples: list[int]) -> BootMsStats:
    if not samples:
        return BootMsStats()
    ordered = sorted(samples)
    return BootMsStats(
        p50=_percentile(ordered, 50),
        p95=_percentile(ordered, 95),
        samples=len(samples),
    )
