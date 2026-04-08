"""Shared Postgres client.

Env-var helpers plus a thin wrapper around ``psycopg`` (v3) for opening
short-lived connections. Used by ``ensure_team_schema`` at startup to
run DDL; heavy CRUD paths (e.g. ``job_service/db.py``) keep their own
pool because DDL doesn't need pooling and the agents image already
pins ``psycopg[binary]``.

Env vars (identical to ``job_service/db.py`` and
``backend/unified_api/postgres_encrypted_credentials.py``):

    POSTGRES_HOST, POSTGRES_PORT, POSTGRES_USER, POSTGRES_PASSWORD,
    POSTGRES_DB

``is_postgres_enabled()`` returns ``True`` only when ``POSTGRES_HOST`` is
set.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Generator
from contextlib import contextmanager
from typing import Optional

logger = logging.getLogger(__name__)

# Track connections currently being used per database so ``close_pool``
# can log anomalies; the module does not keep a real pool open.
_active_conns_lock = threading.Lock()
_active_conns: dict[str, int] = {}


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


def _connect(database: Optional[str] = None):
    """Open a fresh ``psycopg`` connection.

    Raises ``RuntimeError`` when Postgres is disabled or psycopg is not
    installed, so callers fail loudly instead of silently skipping writes.
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


@contextmanager
def get_conn(database: Optional[str] = None) -> Generator:
    """Yield a fresh ``psycopg`` connection for ``database``.

    Commits on clean exit, rolls back on exception, always closes the
    connection. Suitable for startup DDL and infrequent operations; for
    high-throughput CRUD, use a dedicated pool.
    """
    db = database or _default_database()
    conn = _connect(database)
    with _active_conns_lock:
        _active_conns[db] = _active_conns.get(db, 0) + 1
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        try:
            conn.close()
        finally:
            with _active_conns_lock:
                _active_conns[db] = max(0, _active_conns.get(db, 0) - 1)


def close_pool(database: Optional[str] = None) -> None:
    """No-op for the per-call client; kept for lifespan API parity.

    Connections opened by ``get_conn`` are closed automatically when
    their context manager exits. This function exists so team lifespans
    can mirror the ``shared_temporal``/FastAPI shape without special
    casing. Logs a warning if any connections are still reported active.
    """
    with _active_conns_lock:
        dbs = [database] if database is not None else list(_active_conns.keys())
        for db in dbs:
            leaked = _active_conns.get(db, 0)
            if leaked > 0:
                logger.warning(
                    "shared_postgres close_pool: %d active connection(s) reported for database=%s",
                    leaked,
                    db,
                )
            _active_conns.pop(db, None)
