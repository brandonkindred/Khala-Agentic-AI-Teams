"""First-provision seed-pack installation for cognition agents.

A cognition agent declares the seed rule packs it wants in its manifest
(``cognition.rule_packs`` — see :mod:`agent_cognition.manifest_scope`). This
module turns that declaration into installed rules the first time the cognition
core handles the agent, via :func:`ensure_seed_packs_installed`.

The install is **lazy** (driven from the invoke boundary, not a separate
provisioning step), **idempotent** (the underlying
:func:`agent_cognition.rules.store.install_seed_pack` keys each seed rule on a
deterministic ``(agent_id, pack, seed_key)`` id with ``ON CONFLICT DO NOTHING``),
and **best-effort** (a cognition hiccup must never break the invoke). A
per-process memo skips the Postgres round-trip on every invoke after the first
successful pass for an agent; the deterministic ids keep cross-process and
concurrent installs correct on their own, so the memo is only an optimization.

Invariant: this module never raises — every failure path logs and returns ``[]``.
"""

from __future__ import annotations

import logging
import threading

from agent_cognition import manifest_scope
from agent_cognition.memory.store import AgentCognitionStorageUnavailable
from agent_cognition.rules import store
from shared_postgres import is_postgres_enabled

logger = logging.getLogger(__name__)

__all__ = ["ensure_seed_packs_installed"]

# Per-process record of agents whose declared packs we have already installed,
# so the common (already-provisioned) invoke skips the DB round-trip. Guarded by
# a lock because the invoke boundary calls this from a thread pool. Only a clean
# pass records the agent — a tolerated storage outage leaves it absent so a later
# invoke retries.
_PROVISIONED: set[str] = set()
_LOCK = threading.Lock()


def ensure_seed_packs_installed(agent_id: str) -> list[str]:
    """Install the agent's manifest-declared seed rule packs, once, best-effort.

    Preconditions:
        * ``agent_id`` is non-empty.
    Postconditions:
        * No-op returning ``[]`` when Postgres is unconfigured or the agent was
          already provisioned in this process.
        * Otherwise each pack named in the manifest's ``cognition.rule_packs`` is
          installed idempotently; returns the rule ids newly inserted across all
          packs (``[]`` when every declared rule already existed). An unknown
          pack name is logged and skipped — one bad name does not block the rest.
        * The agent is memoized (so later invokes skip the work) only after a
          clean install of a **non-empty** pack list. An empty result is never
          memoized — it is indistinguishable from a transient registry-lookup
          failure (``manifest_scope.rule_packs`` returns ``[]`` for both a
          declared-empty manifest and a failed lookup), so a later invoke re-reads
          the registry and installs the declared packs once it recovers. The
          re-read is a cheap in-process registry call, not a DB round-trip.
        * A storage outage (or any unexpected error) is logged, leaves the memo
          untouched, and still returns ``[]``.
        * Never raises — a cognition failure must not break the invoke.
    """
    assert agent_id, "ensure_seed_packs_installed: agent_id must be non-empty"
    if not is_postgres_enabled():
        return []
    with _LOCK:
        if agent_id in _PROVISIONED:
            return []

    packs = manifest_scope.rule_packs(agent_id)
    if not packs:
        # No declared packs, or a transient registry-lookup failure that yielded
        # an empty list — nothing to install, and memoizing would wrongly mark a
        # failed lookup as provisioned. Return without recording; a real empty
        # declaration just re-reads the cheap registry next invoke.
        return []
    new_ids: list[str] = []
    try:
        for pack in packs:
            try:
                new_ids.extend(store.install_seed_pack(agent_id, pack))
            except store.RuleStoreError:
                logger.warning(
                    "cognition: skipping unknown seed pack %r declared by agent %s", pack, agent_id
                )
    except AgentCognitionStorageUnavailable:
        logger.warning(
            "cognition: seed-pack install for %s deferred (storage unavailable)", agent_id
        )
        return []
    except Exception:
        logger.warning(
            "cognition: seed-pack install for %s failed; continuing", agent_id, exc_info=True
        )
        return []

    with _LOCK:
        _PROVISIONED.add(agent_id)
    if new_ids:
        logger.info("cognition: installed %d seed rule(s) for %s", len(new_ids), agent_id)
    return new_ids


def _reset_provisioned_cache() -> None:
    """Clear the per-process provisioned memo. Test-only hook."""
    with _LOCK:
        _PROVISIONED.clear()
