"""
Agent-level ownership lock for the provisioning/deprovisioning pipeline.

Nothing else in this package stops two Temporal workflows from processing
the same ``agent_id`` at once: ``EnvironmentStore``, ``CredentialStore``, and
``ProvisionerStateStore`` are all keyed solely by ``agent_id``, and every
teardown path (``cleanup_setup``, ``ProvisioningOrchestrator.compensate``,
``ProvisioningOrchestrator.deprovision``) acts on that key alone. Two
concurrent jobs for one ``agent_id`` can therefore interleave: job A's
failure-path rollback can deprovision the Docker container / credentials job
B just created.

This module closes that gap with a persistent ownership record, keyed by
``agent_id``, claimed by an owner token (a provisioning ``job_id`` or a
deprovisioning workflow id) for the *entire* duration of one Temporal
workflow run. A workflow acquires the lock as its first action and releases
it as its last, so every existing agent_id-keyed teardown call becomes safe
by construction: it can only ever run while its own workflow still holds
exclusive ownership of that ``agent_id``, and a second workflow cannot even
begin until the first fully releases.

A Temporal *workflow* must stay side-effect-free/replayable, so it cannot
hold an OS-level lock open across ``await workflow.execute_activity(...)``
calls (each activity may run on a different worker thread/process). The
ownership record is therefore a small JSON file, not a held file descriptor:
``acquire``/``release`` are two independent, short-lived operations (called
from ``acquire_agent_lock_activity`` / ``release_agent_lock_activity``) that
each take a brief cross-process ``fcntl.flock`` just long enough to make
their own read-check-write atomic, mirroring
``CredentialStore._lock_exclusive``. The record itself is written atomically
via tempfile -> fsync -> ``os.replace``, mirroring
``ProvisionerStateStore._save``.

A generous TTL (``LOCK_TTL_S``) is a pure backstop for the rare case a
workflow is hard-terminated (bypassing its ``finally`` release) — normal
success/failure paths always release explicitly and never depend on it.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Optional

from .path_safety import safe_path_component

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platform
    fcntl = None  # type: ignore[assignment]

DEFAULT_LOCK_TTL_SECONDS = 7200


def default_locks_dir() -> Path:
    """Resolve the durable on-disk lock-record directory.

    Preconditions:
        * None.
    Postconditions:
        * Returns ``${AGENT_CACHE:-.agent_cache}/agent_provisioning/locks``
          as a ``Path`` (directory need not exist yet).
    """
    root = Path(os.environ.get("AGENT_CACHE", ".agent_cache"))
    return root / "agent_provisioning" / "locks"


class AgentLockError(RuntimeError):
    """Base error for agent-lock operations."""


class AgentLockBusyError(AgentLockError):
    """Raised by :meth:`AgentLockStore.acquire` when another owner holds the lock.

    Attributes:
        agent_id: The agent whose lock was contended.
        holder: The current (non-expired) owner token, if known.
    """

    def __init__(self, agent_id: str, holder: Optional[str]) -> None:
        self.agent_id = agent_id
        self.holder = holder
        super().__init__(f"agent {agent_id!r} is currently locked by owner {holder!r}")


class AgentLockStore:
    """JSON-backed, cross-process ownership record keyed by ``agent_id``.

    Invariants:
        * At most one owner token is recorded per ``agent_id`` at any time
          (barring TTL-expired records, which any caller may reclaim).
        * A record is only ever removed by :meth:`release` called with the
          owner token that currently holds it, or overwritten by
          :meth:`acquire` when the prior record is absent, expired, or
          already owned by the same token.
    """

    def __init__(
        self,
        storage_dir: Optional[Path] = None,
        ttl_seconds: Optional[int] = None,
    ) -> None:
        """Bind the store to its on-disk directory.

        Preconditions:
            * ``ttl_seconds``, when given, is positive.
        Postconditions:
            * ``self.storage_dir`` exists (created if necessary).
        """
        self.storage_dir = storage_dir or default_locks_dir()
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = ttl_seconds if ttl_seconds is not None else DEFAULT_LOCK_TTL_SECONDS
        assert self.ttl_seconds > 0, "ttl_seconds must be positive"

    # ---- path helpers ----
    def _record_path(self, agent_id: str) -> Path:
        name = safe_path_component(agent_id, kind="agent_id")
        return self.storage_dir / f"{name}.json"

    def _flock_path(self, agent_id: str) -> Path:
        name = safe_path_component(agent_id, kind="agent_id")
        return self.storage_dir / f".{name}.lock"

    # ---- I/O ----
    def _read_record(self, agent_id: str) -> Optional[dict]:
        path = self._record_path(agent_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _write_record(self, agent_id: str, record: dict) -> None:
        path = self._record_path(agent_id)
        fd, tmp_path = tempfile.mkstemp(
            prefix=f".{safe_path_component(agent_id, kind='agent_id')}.",
            suffix=".json",
            dir=str(self.storage_dir),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(record, f, separators=(",", ":"), sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _delete_record(self, agent_id: str) -> None:
        path = self._record_path(agent_id)
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    @staticmethod
    def _lock_exclusive(handle) -> None:
        """Best-effort exclusive advisory lock on an open file ``handle``.

        Serializes the read-check-write below across processes that share
        ``storage_dir`` on a single host. Degrades to a no-op where flock is
        unavailable (non-POSIX, or a filesystem that does not support it) —
        the atomic ``os.replace`` publication still prevents a torn read
        there, leaving only a narrow, environment-specific TOCTOU window.
        """
        if fcntl is None:  # pragma: no cover - non-POSIX platform
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except OSError:  # pragma: no cover - lock unsupported on this filesystem
            pass

    @staticmethod
    def _is_expired(record: dict, now: float) -> bool:
        expires_at = record.get("expires_at")
        return not isinstance(expires_at, (int, float)) or expires_at < now

    # ---- Public API ----
    def acquire(self, agent_id: str, owner: str, *, now: Optional[float] = None) -> None:
        """Claim exclusive ownership of ``agent_id`` for ``owner``.

        Preconditions:
            * ``agent_id`` and ``owner`` are non-empty strings.
        Postconditions:
            * On return, the persisted record's owner is ``owner`` and its
              lease has been (re)issued for ``ttl_seconds`` from ``now``.
            * Idempotent for the current owner: re-acquiring with the same
              ``owner`` renews the lease rather than raising, so a retried
              ``acquire_agent_lock_activity`` for the same job never
              self-deadlocks.
            * Raises :class:`AgentLockBusyError` when a *different*,
              non-expired owner currently holds the lock.
        """
        assert agent_id, "agent_id must be non-empty"
        assert owner, "owner must be non-empty"
        now = time.time() if now is None else now
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self._flock_path(agent_id)
        with open(lock_path, "a+") as handle:
            self._lock_exclusive(handle)
            record = self._read_record(agent_id)
            if (
                record is not None
                and record.get("owner") != owner
                and not self._is_expired(record, now)
            ):
                raise AgentLockBusyError(agent_id, record.get("owner"))
            self._write_record(
                agent_id,
                {"owner": owner, "acquired_at": now, "expires_at": now + self.ttl_seconds},
            )

    def release(self, agent_id: str, owner: str, *, now: Optional[float] = None) -> None:
        """Release ``owner``'s ownership of ``agent_id`` (best-effort, idempotent).

        Preconditions:
            * ``agent_id`` and ``owner`` are non-empty strings.
        Postconditions:
            * The record is removed only if it is still owned by ``owner``.
            * A no-op — never raises — when the record is already absent or
              owned by a different token (release is best-effort cleanup,
              matching ``cleanup_setup``/``ProvisioningOrchestrator.compensate``'s
              existing idiom elsewhere in this package).
        """
        assert agent_id, "agent_id must be non-empty"
        assert owner, "owner must be non-empty"
        lock_path = self._flock_path(agent_id)
        with open(lock_path, "a+") as handle:
            self._lock_exclusive(handle)
            record = self._read_record(agent_id)
            if record is not None and record.get("owner") == owner:
                self._delete_record(agent_id)

    def get_owner(self, agent_id: str, *, now: Optional[float] = None) -> Optional[str]:
        """Return the current non-expired owner of ``agent_id``, or ``None``.

        Lock-free read (mirrors ``ProvisionerStateStore.get``): the record is
        published atomically, so this always observes a whole committed
        snapshot, pre- or post- a concurrent write.
        """
        assert agent_id, "agent_id must be non-empty"
        now = time.time() if now is None else now
        record = self._read_record(agent_id)
        if record is None or self._is_expired(record, now):
            return None
        return record.get("owner")
