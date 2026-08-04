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
registry. DDL lives in ``agent_registry.postgres``; it is registered from the
unified API lifespan **and** ensured lazily on the first write here (see
:func:`_ensure_schema`), so a write from a process that never applied the DDL
(e.g. the standalone ``agentic_team_provisioning`` service) still lands in the
shared table rather than silently degrading the agent to local-only.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import Any, Callable, Optional, TypeVar

from .models import AgentManifest

_T = TypeVar("_T")

logger = logging.getLogger(__name__)

_STORE = "agent_registry_dynamic"
_TABLE = "agent_registry_dynamic_manifests"
# psycopg's %s placeholders only parameterize values, never identifiers, so
# every query below interpolates ``_TABLE`` into the SQL string directly
# (matching the identifier-safety approach in shared.postgres.aggregate).
# ``_TABLE`` is a fixed code literal, never attacker-controlled, but this
# validates it as a bare SQL identifier once at import time so any future
# change to its value (e.g. adding a schema-qualified prefix) that isn't a
# safe identifier fails loudly here instead of silently becoming an
# injection vector at every f-string call site below.
assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", _TABLE), f"_TABLE is not a bare identifier: {_TABLE!r}"

# Small TTL micro-cache for the full-list read (``all()``), which backs the
# catalog list/search/teams endpoints. Point ``get()`` lookups (the invoke /
# provision hot path) are never cached — they must be exact and immediate.
_ALL_CACHE_TTL_S = 2.0
# Guards only the cache STATE (the two globals below) — held briefly, never across
# a Postgres call. A separate lock (_all_cache_refresh_lock) single-flights the
# actual query, so upsert()/delete()'s clear_cache() is never blocked behind a
# concurrent all() query's network round trip.
_all_cache_lock = threading.Lock()
_all_cache_refresh_lock = threading.Lock()
_all_cache: Optional[list[AgentManifest]] = None
_all_cache_at: float = 0.0

# Bounds retries of a write (upsert/delete) whose first attempt hits a transient
# Postgres error, so a brief blip doesn't need AgentRegistry's caller-side
# best-effort degrade-to-local-only path as often. Reads (get/all) are not
# retried here — a failed read already degrades gracefully one level up.
_WRITE_RETRY_ATTEMPTS = 2
_WRITE_RETRY_DELAY_S = 0.05


def _with_retry(fn: Callable[[], _T]) -> _T:
    """Run ``fn()`` (a no-arg write), retrying once on failure before propagating.

    Preconditions:
        * ``fn`` is idempotent (safe to run twice) — both ``upsert``'s
          ``ON CONFLICT DO UPDATE`` and ``delete``'s ``DELETE ... WHERE id = %s``
          qualify.
    Postconditions:
        * Returns ``fn()``'s result on the first success. On the first attempt's
          exception, waits :data:`_WRITE_RETRY_DELAY_S` and tries once more; the
          second attempt's exception (if any) propagates to the caller unchanged.
          Narrows the window in which a purely transient error (a brief connection
          blip) forces the caller's best-effort degrade-to-local-only path.

    ``except Exception`` (not a narrower psycopg error type) is deliberate: ``fn``
    is always one of this module's own ``_do`` closures, never caller-supplied
    business logic, so there is no distinct "programming error" class to exclude
    here — any exception it raises is, by construction, a failure of the one
    Postgres statement it runs.
    """
    for attempt in range(_WRITE_RETRY_ATTEMPTS):
        try:
            return fn()
        except Exception:
            if attempt == _WRITE_RETRY_ATTEMPTS - 1:
                raise
            logger.debug("dynamic store write failed; retrying once", exc_info=True)
            time.sleep(_WRITE_RETRY_DELAY_S)


def _store_active() -> bool:
    """Whether the dynamic Postgres store should be used in this process.

    Postconditions:
        * ``True`` iff Postgres is configured **and** we are not running inside a
          per-invoke sandbox (``SANDBOX_AGENT_ID`` unset). Callers must treat a
          ``False`` result as "in-memory only, exactly as before".
    """
    from shared.postgres import is_postgres_enabled

    return is_postgres_enabled() and not os.environ.get("SANDBOX_AGENT_ID")


# Per-process guard so the first write applies this store's DDL at most once.
_schema_ensured = False
_schema_ensure_lock = threading.Lock()


def _apply_schema_statements(cur) -> None:
    """Run this store's idempotent DDL on an open cursor.

    Preconditions: ``cur`` is a live cursor on a writable connection.
    Postconditions: every ``SCHEMA.statements`` entry has been executed
        (``CREATE … IF NOT EXISTS`` / equivalent). Does not flip
        :data:`_schema_ensured` — callers that may still roll back their
        transaction must leave the guard unset so a later attempt can retry.
    """
    from . import postgres as _pg_schema

    for statement in _pg_schema.SCHEMA.statements:
        cur.execute(statement)


def _ensure_schema() -> None:
    """Idempotently create this store's table before a write, once per process.

    The ``agent_registry_dynamic_manifests`` DDL is normally applied by the
    unified API lifespan, but ``AgentRegistry.register()`` / ``unregister()`` also
    run in *other* processes — notably the standalone ``agentic_team_provisioning``
    service, whose own lifespan registers only its team schema. On a fresh
    Postgres, or if that service handles a generation request before unified-api
    has applied the DDL, the first ``upsert`` / ``delete`` would hit
    ``UndefinedTable`` and the caller would silently degrade the agent to
    local-only (invisible to other workers/sandboxes). Applying the schema here on
    the first write — it is ``CREATE TABLE IF NOT EXISTS``, hence idempotent and
    safe to run alongside the lifespan registration — makes the write path
    self-sufficient regardless of process startup order.

    Postconditions:
        * On success the table exists and the per-process guard is set, so
          subsequent writes skip the DDL. A failure leaves the guard unset (the
          next write retries) and propagates, so the caller still degrades to
          local exactly as before.

    Do **not** call this while holding another pooled connection (e.g. the
    chat-save roster ``conn``): ``register_team_schemas`` opens its own
    ``get_conn()`` and will deadlock under ``POSTGRES_POOL_MAX_SIZE=1``. Shared-
    connection writers use :func:`_apply_schema_statements` on that ``conn``
    instead.
    """
    global _schema_ensured
    if _schema_ensured:
        return
    with _schema_ensure_lock:
        if _schema_ensured:
            return
        from shared.postgres import register_team_schemas

        from . import postgres as _pg_schema

        register_team_schemas(_pg_schema.SCHEMA)
        _schema_ensured = True


def clear_cache() -> None:
    """Drop the ``all()`` micro-cache. Called on writes and by tests.

    Preconditions: none.
    Postconditions:
        * The next ``all()`` call re-queries Postgres rather than serving a cached
          value (modulo a refresh already in flight — see ``all()``'s docstring).
          Acquires only the cheap cache-state lock, never the refresh lock, so a
          slow concurrent Postgres query inside ``all()`` never delays this call.
    """
    global _all_cache, _all_cache_at
    with _all_cache_lock:
        _all_cache = None
        _all_cache_at = 0.0


def upsert(manifest: AgentManifest) -> None:
    """Insert or replace a dynamic manifest by id.

    Preconditions:
        * ``manifest.id`` is a non-empty string (enforced; raises ``ValueError``).
    Postconditions:
        * ``get(manifest.id)`` returns an equal manifest from any worker. A single
          transient failure is retried once (see :func:`_with_retry`) before
          propagating, narrowing the window in which the caller's best-effort
          degrade-to-local-only path is needed.
    """
    from shared.postgres import Json, get_conn
    from shared.postgres.metrics import timed_query

    if not manifest.id:
        raise ValueError("upsert: manifest.id must be non-empty")

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

    _ensure_schema()
    _with_retry(_do)
    clear_cache()


def delete(agent_id: str) -> None:
    """Remove a dynamic manifest by id (no-op if absent).

    Postconditions:
        * ``get(agent_id)`` returns ``None`` afterward on every worker. A single
          transient failure is retried once (see :func:`_with_retry`) before
          propagating, narrowing the window in which a stale row could otherwise
          resurface.
    """
    from shared.postgres import get_conn
    from shared.postgres.metrics import timed_query

    @timed_query(store=_STORE, op="delete")
    def _do() -> None:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(f"DELETE FROM {_TABLE} WHERE id = %s", (agent_id,))

    _ensure_schema()
    _with_retry(_do)
    clear_cache()


def replace_manifests(
    upserts: list[AgentManifest],
    delete_ids: list[str],
    *,
    conn: Any | None = None,
) -> None:
    """Atomically upsert ``upserts`` and delete ``delete_ids``.

    Used by generated-roster replacement so a mid-replace failure cannot leave
    the shared store with some new rows installed and some stale rows already
    deleted while the caller's roster DB transaction rolls back.

    When ``conn`` is provided (chat-save path), statements run on that connection
    and join the caller's open transaction — no nested ``get_conn()`` commit.
    When ``conn`` is ``None``, opens a dedicated transaction (retried once on
    transient failure; see :func:`_with_retry`).

    Preconditions:
        * Every ``manifest.id`` in ``upserts`` is a non-empty string.
        * ``upserts`` ids and ``delete_ids`` are disjoint.
    Postconditions:
        * On success (standalone ``conn is None``): every upserted id is readable
          via :func:`get` on any worker and every ``delete_ids`` entry is gone;
          the dedicated transaction has committed.
        * On success (shared ``conn``): statements are pending on ``conn`` until
          the caller commits; other connections do not see them yet.
        * On any statement failure the active transaction rolls back (standalone)
          or is marked failed for the caller (shared); the exception propagates.
          Clears the ``all()`` micro-cache on success. Standalone path retries
          once on transient failure; shared-``conn`` path does not retry (a
          mid-batch failure would leave the caller's txn aborted). Shared-``conn``
          path never opens a nested pool connection for DDL (applies
          ``IF NOT EXISTS`` statements on ``conn`` when the process guard is
          unset) so ``POSTGRES_POOL_MAX_SIZE=1`` cannot deadlock.
    """
    from shared.postgres import Json, get_conn
    from shared.postgres.metrics import timed_query

    upsert_ids = []
    for m in upserts:
        if not m.id:
            raise ValueError("replace_manifests: every upsert must have a non-empty id")
        upsert_ids.append(m.id)
    overlap = set(upsert_ids) & set(delete_ids)
    if overlap:
        raise ValueError(
            f"replace_manifests: upserts and delete_ids must be disjoint; overlap={sorted(overlap)!r}"
        )

    def _execute(cur) -> None:
        for manifest in upserts:
            payload = manifest.model_dump(mode="json")
            cur.execute(
                f"INSERT INTO {_TABLE} (id, team, tags, manifest, updated_at) "
                "VALUES (%s, %s, %s, %s, NOW()) "
                "ON CONFLICT (id) DO UPDATE SET "
                "team = EXCLUDED.team, tags = EXCLUDED.tags, "
                "manifest = EXCLUDED.manifest, updated_at = NOW()",
                (manifest.id, manifest.team, Json(list(manifest.tags)), Json(payload)),
            )
        for agent_id in delete_ids:
            cur.execute(f"DELETE FROM {_TABLE} WHERE id = %s", (agent_id,))

    if conn is not None:
        # Hold no second pool checkout while the caller already owns ``conn``
        # (deadlock under pool max size 1). Apply DDL on ``conn`` when needed
        # without flipping ``_schema_ensured`` — the outer txn may still roll back.
        if not _schema_ensured:
            with conn.cursor() as cur:
                _apply_schema_statements(cur)
        with conn.cursor() as cur:
            _execute(cur)
        clear_cache()
        return

    _ensure_schema()

    @timed_query(store=_STORE, op="replace_manifests")
    def _do() -> None:
        with get_conn() as owned, owned.cursor() as cur:
            _execute(cur)

    _with_retry(_do)
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
    from shared.postgres import dict_row, get_conn
    from shared.postgres.metrics import timed_query

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

    Single-flight, without blocking writes: a dedicated ``_all_cache_refresh_lock``
    (never held by ``clear_cache()``) serializes concurrent callers racing an
    expired cache behind the one refresh instead of each issuing their own
    duplicate Postgres query — while the cheap cache-state lock
    (``_all_cache_lock``) is only ever held briefly to read or write the two cache
    globals, never across the query itself. This means a concurrent
    ``upsert()``/``delete()``'s ``clear_cache()`` is never blocked behind this
    call's Postgres round trip, even during a slow refresh. The narrow tradeoff: if
    ``clear_cache()`` fires while a refresh is already in flight, that refresh's
    write-back (of pre-clear data) can briefly re-populate the cache — a self
    -healing staleness bounded by the next ``_ALL_CACHE_TTL_S`` window, not the
    unbounded-write-blocking hazard the single-lock version had.
    """
    global _all_cache, _all_cache_at

    from shared.postgres import dict_row, get_conn
    from shared.postgres.metrics import timed_query

    def _fresh_copy() -> Optional[list[AgentManifest]]:
        with _all_cache_lock:
            if _all_cache is not None and (time.monotonic() - _all_cache_at) < _ALL_CACHE_TTL_S:
                return list(_all_cache)
        return None

    cached = _fresh_copy()
    if cached is not None:
        return cached

    with _all_cache_refresh_lock:
        # Double-checked: another thread may have completed a refresh while this
        # one waited for the refresh lock.
        cached = _fresh_copy()
        if cached is not None:
            return cached

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
    from shared.postgres import dict_row, get_conn
    from shared.postgres.metrics import timed_query

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
