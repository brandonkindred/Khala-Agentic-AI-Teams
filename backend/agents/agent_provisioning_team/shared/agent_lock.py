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
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Optional

from .fencing import StaleFencingTokenError
from .path_safety import safe_path_component

# Re-exported for callers that import it from here (this module is the one that
# *detects* a stale lock token via :meth:`AgentLockStore.check_fencing_token`).
# It is the SAME class the resource stores raise — deliberately one type, not
# two same-named ones — so ``non_retryable_error_types=["StaleFencingTokenError"]``
# and the workflow's cause-chain check cover both the lock-level and the
# store-level rejection without depending on a name coincidence.
__all__ = ["StaleFencingTokenError"]

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platform
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

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
        * A record file, once created, is never deleted: :meth:`release`
          force-expires it (in place) rather than removing it, and
          :meth:`acquire` overwrites it when the prior record is absent,
          expired, or already owned by the same token. This keeps each
          record's fencing token (see :meth:`acquire`) monotonically
          increasing for the full lifetime of its ``agent_id``, not just
          within one acquire/release cycle.
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
        """Return ``agent_id``'s record, or ``None`` only if none exists.

        Preconditions:
            * None.
        Postconditions:
            * Returns ``None`` when no record file exists for ``agent_id``
              (the safe, unlocked case).
            * Raises :class:`AgentLockError` when a record file exists but
              cannot be read or parsed — this store exists to guarantee
              mutual exclusion, so a present-but-unreadable record must fail
              closed (deny/retry) rather than be silently treated as absent,
              which would let a second caller steal a lock a live owner
              still holds. ``_write_record``'s atomic publish makes this
              exceedingly rare in practice; recovering from it (if the file
              is genuinely corrupt, not just a transient read glitch) is an
              operator action — inspect and remove the record file.
        """
        path = self._record_path(agent_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise AgentLockError(
                f"lock record for {agent_id!r} exists but could not be read: {e}"
            ) from e

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
    def acquire(self, agent_id: str, owner: str, *, now: Optional[float] = None) -> int:
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
            * Returns the fencing token now associated with ``agent_id`` — a
              per-``agent_id`` monotonic counter, persisted alongside the
              lease. It is unchanged from the immediately-prior acquire only
              when this call is a *live* same-owner renewal; it is minted as
              ``prior_token + 1`` (or ``1`` if no prior record ever existed)
              in every other accepted case, including a reclaim-after-expiry
              by the *same* owner. That case must still mint a new token: a
              resuming caller cannot distinguish "nobody touched this lock
              while I was gone" from "someone acquired and released it while
              I was gone" — both read identically as "expired" — so treating
              a same-owner-but-expired reclaim as a free renewal would
              reopen the exact gap fencing tokens exist to close. Callers
              (workflows) must use the most recently returned token for
              every subsequent privileged operation, not a value captured
              earlier in their run, since a late renewal that lands just
              after expiry still mints a new one via this same rule.
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
            live_renewal = (
                record is not None
                and record.get("owner") == owner
                and not self._is_expired(record, now)
            )
            prior_token = record.get("fencing_token") if record is not None else None
            if live_renewal and isinstance(prior_token, int):
                token = prior_token
            else:
                token = (prior_token + 1) if isinstance(prior_token, int) else 1
            self._write_record(
                agent_id,
                {
                    "owner": owner,
                    "acquired_at": now,
                    "expires_at": now + self.ttl_seconds,
                    "fencing_token": token,
                },
            )
            return token

    def release(
        self,
        agent_id: str,
        owner: str,
        *,
        now: Optional[float] = None,
        fencing_token: Optional[int] = None,
    ) -> None:
        """Release ``owner``'s ownership of ``agent_id`` (best-effort, idempotent).

        Preconditions:
            * ``agent_id`` and ``owner`` are non-empty strings.
        Postconditions:
            * The record's lease is force-expired only if it is still owned
              by ``owner`` — never deleted. Preserving the record (rather
              than removing it, as this method did before fencing tokens
              existed) keeps the token monotonic across a release/re-acquire
              cycle: the next :meth:`acquire` for this ``agent_id`` mints
              from the real prior high-water mark instead of restarting at
              1. Observable behavior via :meth:`get_owner`/:meth:`acquire`
              is unchanged — an expired record is indistinguishable from an
              absent one to every existing caller.
            * A no-op — never raises — when the record is already absent,
              owned by a different token, or ``fencing_token`` is given and
              is lower than the record's current token (release is
              best-effort cleanup, matching
              ``cleanup_setup``/``ProvisioningOrchestrator.compensate``'s
              existing idiom elsewhere in this package). The token check is
              defense-in-depth, not load-bearing for the race this module
              defends against: the owner-string check above already rejects
              a stale caller, since ``owner`` and ``token`` are always
              advanced together by the same :meth:`acquire` call.
            * Raises :class:`AgentLockError` (same fail-closed contract as
              :meth:`acquire`) when a record exists but cannot be read — we
              cannot verify it is safe to expire, so it is left untouched
              rather than guessed at.
        """
        assert agent_id, "agent_id must be non-empty"
        assert owner, "owner must be non-empty"
        now = time.time() if now is None else now
        lock_path = self._flock_path(agent_id)
        with open(lock_path, "a+") as handle:
            self._lock_exclusive(handle)
            record = self._read_record(agent_id)
            if record is None or record.get("owner") != owner:
                return
            current_token = record.get("fencing_token")
            if (
                fencing_token is not None
                and isinstance(current_token, int)
                and fencing_token < current_token
            ):
                return
            self._write_record(
                agent_id,
                {
                    "owner": owner,
                    "acquired_at": record.get("acquired_at", now),
                    # Strictly less than `now`, not equal: _is_expired uses a
                    # strict "<" comparison, so expires_at = now would read as
                    # "not yet expired" for a get_owner(now=<same value>) call.
                    "expires_at": now - 1,
                    "fencing_token": current_token if isinstance(current_token, int) else 1,
                },
            )

    def get_owner(self, agent_id: str, *, now: Optional[float] = None) -> Optional[str]:
        """Return the current non-expired owner of ``agent_id``, or ``None``.

        Lock-free read (mirrors ``ProvisionerStateStore.get``): the record is
        published atomically, so this always observes a whole committed
        snapshot, pre- or post- a concurrent write. Raises
        :class:`AgentLockError` when a record exists but cannot be read
        (same fail-closed contract as :meth:`acquire`/:meth:`release`).
        """
        assert agent_id, "agent_id must be non-empty"
        now = time.time() if now is None else now
        record = self._read_record(agent_id)
        if record is None or self._is_expired(record, now):
            return None
        return record.get("owner")

    def check_fencing_token(self, agent_id: str, token: int) -> None:
        """Reject a fencing token superseded by a later acquisition.

        Preconditions:
            * ``agent_id`` is non-empty.
            * ``token`` is the caller's own fencing token from a prior
              :meth:`acquire` call.
        Postconditions:
            * A no-op when no record exists for ``agent_id`` — nothing to
              compare against, mirroring :meth:`get_owner`'s "absent means
              safe" contract.
            * A no-op when ``token`` is greater than or equal to the
              record's persisted ``fencing_token``.
            * Raises :class:`StaleFencingTokenError` when ``token`` is
              strictly less than the record's persisted ``fencing_token``:
              proof a different, later owner has since reclaimed
              ``agent_id``'s lease (:meth:`acquire` only increments the
              token for a *different* owner — a same-owner renewal leaves
              it unchanged), so the caller's own lease is stale.
            * Lock-free read (mirrors :meth:`get_owner`): does not consider
              TTL expiry, only the persisted ``fencing_token`` high-water
              mark — a lease merely past its TTL but not yet reclaimed by
              anyone else still has the caller's own token as the highest
              recorded value, so it is correctly treated as current here.
            * Raises :class:`AgentLockError` when a record exists but
              cannot be read (same fail-closed contract as
              :meth:`acquire`/:meth:`release`/:meth:`get_owner`).
        """
        assert agent_id, "agent_id must be non-empty"
        record = self._read_record(agent_id)
        if record is None:
            return
        current_token = record.get("fencing_token")
        if isinstance(current_token, (int, float)) and token < current_token:
            current_token = int(current_token)
            logger.error(
                "Stale fencing token rejected for agent_id=%s: presented token=%s, current token=%s",
                agent_id,
                token,
                current_token,
            )
            raise StaleFencingTokenError(
                agent_id=agent_id,
                resource="agent_lock",
                provided_token=token,
                current_token=current_token,
            )
