"""Bounded reaping of fencing tombstones and force-expired lock records.

Fencing tokens made every teardown path *preserve* state instead of deleting
it: :meth:`AgentLockStore.release` force-expires its record in place rather
than removing it, and :meth:`ProvisionerStateStore.delete` /
:meth:`CredentialStore.delete_credentials` / :meth:`EnvironmentStore.remove`
leave a minimal tombstone that keeps the high-water mark. That preservation is
load-bearing — it is what lets a resource reject a resumed-but-stale worker's
lower-token write. But kept *forever* it grows the on-disk footprint by one
small record per distinct ``agent_id`` without bound, which matters for
systems that mint a fresh ``agent_id`` per ephemeral agent.

This module reclaims that space *safely*. A tombstone / expired record only
needs to outlive any workflow that could still legitimately present a *lower*
token for the same ``agent_id``. That upper bound is the total time a
provisioning/deprovisioning workflow can stay resumable — the sum of its
per-activity ``schedule_to_close_timeout``s (hours) — **not** Temporal's much
longer history retention: a worker that stays down past every activity's
timeout has those activities failed and the workflow terminated, so it can
never come back and replay a stale write. Once a record has gone
``AGENT_STATE_RETENTION_S`` (default 7 days, floored at 1 day) without any
write, no such resumable stale worker can still exist, so dropping it cannot
reopen the cross-owner teardown race fencing tokens defend against.

Preconditions (for every reaper here):
    * Only records untouched for at least :func:`state_retention_seconds`
      are eligible, and only ones whose *content* confirms they are a
      tombstone / force-expired lease (never a live record).
Postconditions:
    * Reaping is best-effort and never raises; a record that cannot be read
      or removed is left in place.
    * Reaping is idempotent and order-independent across the stores.

Invariants:
    * A live lease, or a resource record still describing live infrastructure,
      is never removed regardless of age.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Safe floor: reaping anything younger than this risks dropping a tombstone a
# still-resumable stale worker depends on. One day is comfortably above the
# worst-case workflow lifetime (per-activity PHASE_TIMEOUT summed across the
# ~10 sequential activities of a provisioning run, plus retries — hours).
_RETENTION_FLOOR_S = 24 * 3600
_DEFAULT_RETENTION_S = 7 * 24 * 3600

# Throttle so the opportunistic sweep wired into the lock-release activity runs
# at most this often per worker process, keeping its directory scans off the
# per-workflow hot path. Best-effort state, reset per process.
_REAP_MIN_INTERVAL_S = 3600
_last_reap_at: Optional[float] = None


def state_retention_seconds() -> float:
    """Return the tombstone/expired-record retention window in seconds.

    Preconditions:
        * None.
    Postconditions:
        * Reads ``AGENT_STATE_RETENTION_S`` (seconds). Garbage or a
          non-positive value falls back to the 7-day default; any value below
          the 1-day safety floor is clamped up to it, since a shorter window
          could reap a record a resumable stale worker still needs.
    """
    raw = os.environ.get("AGENT_STATE_RETENTION_S")
    if raw is None:
        return float(_DEFAULT_RETENTION_S)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return float(_DEFAULT_RETENTION_S)
    if value <= 0:
        return float(_DEFAULT_RETENTION_S)
    return max(value, float(_RETENTION_FLOOR_S))


def reap_stale_agent_state(*, now: Optional[float] = None, force: bool = False) -> None:
    """Best-effort, throttled sweep of the per-agent lock / env / credential stores.

    Preconditions:
        * Safe to call from any activity worker (uses wall-clock; never from a
          Temporal *workflow*, which must stay deterministic).
    Postconditions:
        * At most once per :data:`_REAP_MIN_INTERVAL_S` per process (unless
          ``force``), instantiates the three per-agent-file stores with their
          default directories and calls each store's ``reap_stale``. Every
          store call is wrapped so one failure never blocks the others and the
          caller is never affected. ``ProvisionerStateStore`` is intentionally
          excluded: it self-reaps aged tombstone rows inside ``_save`` on every
          write, so its single consolidated file needs no directory scan here.
    """
    global _last_reap_at
    now = time.time() if now is None else now
    if not force and _last_reap_at is not None and (now - _last_reap_at) < _REAP_MIN_INTERVAL_S:
        return
    _last_reap_at = now
    retention_s = state_retention_seconds()

    # Lazy imports: this module is imported by the stores' package siblings, and
    # keeping the store imports out of module load avoids any import-order cycle.
    try:
        from .agent_lock import AgentLockStore

        AgentLockStore().reap_stale(now=now, retention_s=retention_s)
    except Exception:  # noqa: BLE001 - best-effort reclamation, never fatal
        logger.debug("agent-lock reap skipped", exc_info=True)
    try:
        from .environment_store import EnvironmentStore

        EnvironmentStore().reap_stale(now=now, retention_s=retention_s)
    except Exception:  # noqa: BLE001
        logger.debug("environment-store reap skipped", exc_info=True)
    try:
        from .credential_store import CredentialStore

        CredentialStore().reap_stale(now=now, retention_s=retention_s)
    except Exception:  # noqa: BLE001
        logger.debug("credential-store reap skipped", exc_info=True)
