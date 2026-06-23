"""Shared Postgres client.

Env-var helpers plus a process-wide ``psycopg_pool.ConnectionPool`` per
database. ``get_conn()`` acquires a pooled connection; ``close_pool()``
tears the pools down at shutdown. Used by ``ensure_team_schema`` at
startup and by team stores on hot paths.

Env vars (identical to ``job_service/db.py`` and
``backend/unified_api/postgres_encrypted_credentials.py``):

    POSTGRES_HOST, POSTGRES_PORT, POSTGRES_USER, POSTGRES_PASSWORD,
    POSTGRES_DB

Pool sizing:

    POSTGRES_POOL_MIN_SIZE  (default 2)
    POSTGRES_POOL_MAX_SIZE  (default 10)

``is_postgres_enabled()`` returns ``True`` only when ``POSTGRES_HOST`` is
set.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Generator
from contextlib import contextmanager
from typing import Literal, Optional

from shared_env import parse_int

logger = logging.getLogger(__name__)

# Storage-reachability classification shared by the settings surfaces (LLM Provider
# page, GitHub integration panel) so the three-state contract is single-sourced.
StorageStatus = Literal["available", "unconfigured", "unreachable"]

# Per-database connection pools. Created lazily on first ``get_conn`` call
# for that database name.
_pools_lock = threading.Lock()
_pools: dict[str, object] = {}  # database name -> ConnectionPool


def is_postgres_enabled() -> bool:
    """True when ``POSTGRES_HOST`` is set (e.g. in the Docker stack)."""
    return bool(os.getenv("POSTGRES_HOST", "").strip())


def _default_database() -> str:
    return os.environ.get("POSTGRES_DB", "postgres")


def _connect_timeout() -> int:
    """Return the libpq ``connect_timeout`` (seconds) for new connections.

    Preconditions: none.
    Postconditions: returns a positive int from ``POSTGRES_CONNECT_TIMEOUT_S``
        (default 3, floored to 1). Bounds how long a TCP connect to a down or
        unreachable host can hang before psycopg gives up — without it, opening a
        pool (``open=True``) against an unreachable host can block far longer than
        any caller's own timeout, which is exactly the failure ``check_connection``
        must surface quickly.
    """
    return parse_int("POSTGRES_CONNECT_TIMEOUT_S", 3, minimum=1)


def connect_timeout() -> int:
    """Public accessor for the libpq ``connect_timeout`` (``POSTGRES_CONNECT_TIMEOUT_S``).

    Preconditions: none.
    Postconditions: returns the shared connect-timeout seconds so other modules that
        build their own DSN (e.g. the unified-API credential store) bound the connect
        with the *same* value as the pool, instead of re-deriving the default and
        drifting. Never raises.
    """
    return _connect_timeout()


def _kv(value: str) -> str:
    """Quote a libpq keyword/value string when it needs it.

    Preconditions: ``value`` is a string.
    Postconditions: returns ``value`` unchanged when it is a non-empty run of
        ordinary characters; otherwise returns it single-quoted with ``\\`` and ``'``
        backslash-escaped, per libpq keyword/value rules. "Needs quoting" means the
        value is empty, or contains a single-quote, a backslash, or ANY whitespace —
        libpq terminates a keyword/value token on any whitespace (space, tab, newline,
        CR, …), not just an ASCII space, so a ``POSTGRES_PASSWORD`` with a tab would
        otherwise terminate early and corrupt every following keyword (including the
        ``connect_timeout`` this DSN appends). Never raises.
    """
    if value and not (any(c.isspace() for c in value) or "'" in value or "\\" in value):
        return value
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _dsn(database: Optional[str] = None) -> str:
    """Build a libpq DSN for ``database`` (defaults to ``POSTGRES_DB``)."""
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    user = os.environ.get("POSTGRES_USER", "postgres")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    dbname = database or _default_database()
    return (
        f"host={_kv(host)} port={_kv(port)} dbname={_kv(dbname)} "
        f"user={_kv(user)} password={_kv(password)} connect_timeout={_connect_timeout()}"
    )


def dsn(database: Optional[str] = None) -> str:
    """Public accessor for the shared libpq keyword DSN.

    Preconditions: none.
    Postconditions: returns the exact DSN the connection pool uses — every field
        ``_kv``-escaped and carrying ``connect_timeout`` — so a module that opens its
        own ``psycopg`` connection outside the pool (e.g. the unified-API credential
        store) builds an identically-escaped DSN. One DSN builder, no drift between the
        reachability probe and the live read. Never raises.
    """
    return _dsn(database)


def _pool_sizes() -> tuple[int, int]:
    """Return ``(min_size, max_size)`` for the pool, from env vars.

    Postconditions: both sizes are at least 1 (a zero/negative override is
    clamped up, so the pool can never be configured empty), and
    ``max_size >= min_size``.
    """
    min_size = parse_int("POSTGRES_POOL_MIN_SIZE", 2, minimum=1)
    max_size = parse_int("POSTGRES_POOL_MAX_SIZE", 10, minimum=1)
    if max_size < min_size:
        max_size = min_size
    return min_size, max_size


def _connect(database: Optional[str] = None):
    """Open a fresh (unpooled) ``psycopg`` connection.

    Used for the initial DDL ``ensure_team_schema`` path and as a test
    seam. Raises ``RuntimeError`` when Postgres is disabled or psycopg
    is not installed, so callers fail loudly instead of silently
    skipping writes.
    """
    if not is_postgres_enabled():
        raise RuntimeError("POSTGRES_HOST is not set; cannot open a Postgres connection.")
    try:
        import psycopg
    except ImportError as e:
        raise RuntimeError(
            "psycopg is not installed; install psycopg[binary] to use shared_postgres."
        ) from e
    return psycopg.connect(_dsn(database))


def _get_or_create_pool(database: Optional[str] = None):
    """Return (creating if needed) the ``ConnectionPool`` for ``database``.

    Raises ``RuntimeError`` when Postgres is disabled or ``psycopg_pool``
    is not installed.
    """
    if not is_postgres_enabled():
        raise RuntimeError("POSTGRES_HOST is not set; cannot open a Postgres connection.")

    db = database or _default_database()
    with _pools_lock:
        pool = _pools.get(db)
        if pool is not None:
            return pool
        try:
            from psycopg_pool import ConnectionPool
        except ImportError as e:
            raise RuntimeError(
                "psycopg_pool is not installed; install psycopg_pool to use shared_postgres."
            ) from e
        min_size, max_size = _pool_sizes()
        pool = ConnectionPool(
            conninfo=_dsn(database),
            min_size=min_size,
            max_size=max_size,
            open=True,
            name=f"shared_postgres[{db}]",
        )
        _pools[db] = pool
        logger.info(
            "shared_postgres pool opened: database=%s min_size=%d max_size=%d",
            db,
            min_size,
            max_size,
        )
        return pool


@contextmanager
def get_conn(database: Optional[str] = None) -> Generator:
    """Yield a pooled ``psycopg`` connection for ``database``.

    Commits on clean exit, rolls back on exception, always returns the
    connection to the pool. The first call for a given ``database``
    lazily creates the pool.
    """
    pool = _get_or_create_pool(database)
    # ``ConnectionPool.connection()`` is itself a context manager that
    # commits on clean exit, rolls back on exception, and returns the
    # connection to the pool.
    with pool.connection() as conn:
        yield conn


def check_connection(database: Optional[str] = None, *, timeout_s: float = 1.5) -> bool:
    """Return True iff Postgres is enabled and answers ``SELECT 1`` quickly.

    A real connectivity probe, distinct from :func:`is_postgres_enabled` (which only
    checks that ``POSTGRES_HOST`` is set). Callers use the pair to tell three states
    apart: not-configured (``is_postgres_enabled()`` false), configured-but-unreachable
    (``is_postgres_enabled()`` true, this false), and healthy (both true).

    Preconditions: none.
    Postconditions: returns ``False`` immediately when Postgres is disabled; otherwise
        acquires a pooled connection (waiting at most ``timeout_s`` for a free slot,
        and bounded on the TCP connect by ``POSTGRES_CONNECT_TIMEOUT_S``), runs
        ``SELECT 1``, and returns whether the result is ``(1,)``. Swallows every
        error (disabled, psycopg missing, host down, pool exhausted, timeout) and
        returns ``False`` — never raises. This is a read-only probe with no side
        effects beyond opening/reusing the shared pool.
    """
    if not is_postgres_enabled():
        return False
    try:
        # Reach through ``_get_or_create_pool`` (rather than ``get_conn``) so the
        # acquisition can be bounded by ``timeout_s``; the public context manager
        # exposes no timeout knob. Mirrors the bounded probe in the unified API
        # health loop, which now delegates here.
        pool = _get_or_create_pool(database)
        with pool.connection(timeout=timeout_s) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            row = cur.fetchone()
            return row is not None and row[0] == 1
    except Exception:  # noqa: BLE001 - a probe must never raise; any failure is "unreachable"
        return False


def resolve_storage_status(*, timeout_s: float = 1.5) -> StorageStatus:
    """Classify the runtime store into one of three states for operator surfaces.

    The single source of truth for the LLM Provider page and the GitHub integration
    panel, so their three-state contract cannot drift.

    Preconditions: none.
    Postconditions: returns ``"unconfigured"`` when ``POSTGRES_HOST`` is unset,
        ``"unreachable"`` when it is set but :func:`check_connection` (a bounded
        ``SELECT 1``) fails, and ``"available"`` when the store answers. Never raises.
        Runs blocking I/O (the probe), so async callers must offload it (and bound the
        offload, since a connection that stalls *after* connect is not covered by
        ``connect_timeout``).
    """
    if not is_postgres_enabled():
        return "unconfigured"
    return "available" if check_connection(timeout_s=timeout_s) else "unreachable"


def close_pool(database: Optional[str] = None) -> None:
    """Close and drop the connection pool(s) opened by ``get_conn``.

    Called from FastAPI lifespan shutdown. Safe to call multiple times;
    safe to call when no pool was ever opened.
    """
    with _pools_lock:
        if database is not None:
            dbs = [database]
        else:
            dbs = list(_pools.keys())
        for db in dbs:
            pool = _pools.pop(db, None)
            if pool is None:
                continue
            try:
                pool.close()
                logger.info("shared_postgres pool closed: database=%s", db)
            except Exception as e:
                logger.warning("shared_postgres pool close failed: database=%s error=%s", db, e)
