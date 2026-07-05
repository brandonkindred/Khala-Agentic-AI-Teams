"""Postgres-backed persistence for dynamically registered agent manifests.

The base :class:`~agent_registry.loader.AgentRegistry` loads hand-authored
manifests from disk YAML into an in-memory dict. That is per-process, so a
manifest registered at runtime (Agent Studio save, ``agentic_team_provisioning``
generated agents) is invisible to the other uvicorn workers and to the per-invoke
sandbox. This module gives those *dynamic* manifests a shared home in Postgres so
every worker resolves them coherently.

Trust / scope boundary:
    * Only **dynamic** ids are stored here — disk-YAML catalog ids never touch
      Postgres (they are already on every worker's disk and in every sandbox
      image). ``AgentRegistry`` enforces that split via its ``_static_ids`` set.
    * This store is **disabled inside a sandbox**. A sandbox's ``POSTGRES_HOST``
      points at its own fresh, isolated Postgres (no ``agent_registry_*`` table),
      so using it there would be wrong. The sandbox receives its one manifest via
      provision-time injection (see ``agent_sandbox_runtime.entrypoint``), never
      via this store. ``_store_active()`` is the guard.

All functions raise on Postgres failure; ``AgentRegistry`` wraps every call so a
Postgres outage degrades to local in-memory resolution rather than breaking the
registry. DDL lives in ``agent_registry.postgres`` and is registered from the
unified API lifespan.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

from .models import AgentManifest

logger = logging.getLogger(__name__)

_STORE = "agent_registry_dynamic"
_TABLE = "agent_registry_dynamic_manifests"

# Small TTL micro-cache for the full-list read (``all()``), which backs the
# catalog list/search/teams endpoints. Point ``get()`` lookups (the invoke /
# provision hot path) are never cached — they must be exact and immediate.
_ALL_CACHE_TTL_S = 2.0
_all_cache_lock = threading.Lock()
_all_cache: Optional[list[AgentManifest]] = None
_all_cache_at: float = 0.0


def _store_active() -> bool:
    """Whether the dynamic Postgres store should be used in this process.

    Postconditions:
        * ``True`` iff Postgres is configured **and** we are not running inside a
          per-invoke sandbox (``SANDBOX_AGENT_ID`` unset). Callers must treat a
          ``False`` result as "in-memory only, exactly as before".
    """
    from shared_postgres import is_postgres_enabled

    return is_postgres_enabled() and not os.environ.get("SANDBOX_AGENT_ID")


def clear_cache() -> None:
    """Drop the ``all()`` micro-cache. Called on writes and by tests."""
    global _all_cache, _all_cache_at
    with _all_cache_lock:
        _all_cache = None
        _all_cache_at = 0.0


def upsert(manifest: AgentManifest) -> None:
    """Insert or replace a dynamic manifest by id.

    Preconditions:
        * ``manifest.id`` is non-empty.
    Postconditions:
        * ``get(manifest.id)`` returns an equal manifest from any worker.
    """
    from shared_postgres import Json, get_conn
    from shared_postgres.metrics import timed_query

    @timed_query(store=_STORE, op="upsert")
    def _do() -> None:
        payload = manifest.model_dump(mode="json")
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {_TABLE} (id, team, tags, manifest, updated_at) "
                "VALUES (%s, %s, %s, %s, NOW()) "
                "ON CONFLICT (id) DO UPDATE SET "
                "team = EXCLUDED.team, tags = EXCLUDED.tags, "
                "manifest = EXCLUDED.manifest, updated_at = NOW()",
                (manifest.id, manifest.team, Json(list(manifest.tags)), Json(payload)),
            )

    _do()
    clear_cache()


def delete(agent_id: str) -> None:
    """Remove a dynamic manifest by id (no-op if absent).

    Postconditions:
        * ``get(agent_id)`` returns ``None`` afterward on every worker.
    """
    from shared_postgres import get_conn
    from shared_postgres.metrics import timed_query

    @timed_query(store=_STORE, op="delete")
    def _do() -> None:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(f"DELETE FROM {_TABLE} WHERE id = %s", (agent_id,))

    _do()
    clear_cache()


def get(agent_id: str) -> AgentManifest | None:
    """Return the dynamic manifest for ``agent_id``, or ``None`` if unknown.

    Preconditions:
        * ``agent_id`` is a string; Postgres is reachable (callers wrap failures).
    Postconditions:
        * Returns the persisted manifest (validated from its JSON dump) when a row
          exists, else ``None``. Never cached — this is the exact, immediate read
          the invoke / provision path depends on for cross-worker save→invoke
          coherence.
    """
    from shared_postgres import dict_row, get_conn
    from shared_postgres.metrics import timed_query

    @timed_query(store=_STORE, op="get")
    def _do() -> AgentManifest | None:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(f"SELECT manifest FROM {_TABLE} WHERE id = %s", (agent_id,))
            row = cur.fetchone()
        if row is None:
            return None
        return AgentManifest.model_validate(row["manifest"])

    return _do()


def all() -> list[AgentManifest]:  # noqa: A001 - mirrors AgentRegistry.all()
    """Return every dynamic manifest.

    Preconditions:
        * Postgres is reachable (callers wrap failures).
    Postconditions:
        * Returns all persisted dynamic manifests, id-ordered. Result is micro-cached
          for ``_ALL_CACHE_TTL_S`` (~2s) to shield the catalog list/search/teams
          reads.

    Cross-worker consistency: the cache is **process-local**. It is cleared eagerly
    only on this worker's own :func:`upsert` / :func:`delete` (via
    :func:`clear_cache`) — there is no cross-worker invalidation. A write on another
    worker is therefore reflected here only after this worker's cache entry expires,
    i.e. within ``_ALL_CACHE_TTL_S``. (This is TTL staleness, not Postgres
    replication lag — all workers read the same primary.) The point-lookup
    :func:`get` is uncached, so cross-worker save→resolve is immediate; only the
    catalog *list* is eventually-consistent within the TTL window.
    """
    global _all_cache, _all_cache_at
    now = time.monotonic()
    with _all_cache_lock:
        if _all_cache is not None and (now - _all_cache_at) < _ALL_CACHE_TTL_S:
            return list(_all_cache)

    from shared_postgres import dict_row, get_conn
    from shared_postgres.metrics import timed_query

    @timed_query(store=_STORE, op="all")
    def _do() -> list[AgentManifest]:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(f"SELECT manifest FROM {_TABLE} ORDER BY id")
            rows = cur.fetchall()
        return [AgentManifest.model_validate(r["manifest"]) for r in rows]

    manifests = _do()
    with _all_cache_lock:
        _all_cache = list(manifests)
        _all_cache_at = time.monotonic()
    return manifests


def manifests_with_prefix(prefix: str) -> list[AgentManifest]:
    """Return dynamic manifests whose id starts with ``prefix``.

    Used by ``register_team_manifests``' stale-roster cleanup so removed/renamed
    generated agents are dropped across workers, not just the one that ran the
    generation.

    Preconditions:
        * ``prefix`` is a string; Postgres is reachable (callers wrap failures).
    Postconditions:
        * Returns every persisted dynamic manifest whose id starts with ``prefix``
          (id-ordered), matching literally — the prefix's ``%``/``_``/``\\`` LIKE
          metachars are escaped so an id containing them can't broaden the match.
    """
    from shared_postgres import dict_row, get_conn
    from shared_postgres.metrics import timed_query

    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    @timed_query(store=_STORE, op="manifests_with_prefix")
    def _do() -> list[AgentManifest]:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"SELECT manifest FROM {_TABLE} WHERE id LIKE %s ESCAPE '\\' ORDER BY id",
                (escaped + "%",),
            )
            rows = cur.fetchall()
        return [AgentManifest.model_validate(r["manifest"]) for r in rows]

    return _do()
