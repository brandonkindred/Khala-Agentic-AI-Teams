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
from typing import Any, Optional

from shared_env import parse_int

logger = logging.getLogger(__name__)

# Per-database connection pools. Created lazily on first ``get_conn`` call
# for that database name.
_pools_lock = threading.Lock()
_pools: dict[str, object] = {}  # database name -> ConnectionPool


def is_postgres_enabled() -> bool:
    """True when ``POSTGRES_HOST`` is set (e.g. in the Docker stack)."""
    return bool(os.getenv("POSTGRES_HOST", "").strip())


def _default_database() -> str:
    return os.environ.get("POSTGRES_DB", "postgres")


def _dsn(database: Optional[str] = None) -> str:
    """Build a libpq DSN for ``database`` (defaults to ``POSTGRES_DB``)."""
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    user = os.environ.get("POSTGRES_USER", "postgres")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    dbname = database or _default_database()
    return f"host={host} port={port} dbname={dbname} user={user} password={password}"


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


@contextmanager
def pg_cursor(
    *, dict_rows: bool = False, database: Optional[str] = None
) -> Generator[Optional[Any], None, None]:
    """Yield a cursor for a short transaction, or ``None`` when Postgres is off.

    Centralizes the ``is_postgres_enabled()`` guard plus the
    ``get_conn() -> conn.cursor()`` acquisition that every team's store otherwise
    repeats. Yields ``None`` (rather than raising) when Postgres is unconfigured,
    so a caller degrades to its no-op / empty result; operational errors raised
    while opening the connection or inside the ``with`` block propagate to the
    caller, which logs them and returns its own empty value — keeping each
    store's best-effort contract and its specific debug message.

    Preconditions:
        - Used as a context manager: ``with pg_cursor() as cur:``; the caller
          checks ``cur is None`` before using it.
    Postconditions:
        - When Postgres is disabled, yields ``None`` and opens no connection.
        - Otherwise yields a live cursor inside a :func:`get_conn` transaction (a
          ``dict_row`` row factory when ``dict_rows=True``); the cursor and
          connection are closed / returned to the pool on exit, committing on a
          clean exit (including an early ``return`` from the ``with`` block) and
          rolling back if an exception propagates.
    """
    if not is_postgres_enabled():
        yield None
        return
    with get_conn(database) as conn:
        if dict_rows:
            from psycopg.rows import dict_row

            with conn.cursor(row_factory=dict_row) as cur:
                yield cur
        else:
            with conn.cursor() as cur:
                yield cur


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
