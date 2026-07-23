"""Shared Postgres access helpers for branding_team's store classes.

Consumed by ``store.py`` (``BrandingStore``) and ``api/state.py``
(``BrandingSessionStore``); ``assistant/store.py`` still hand-writes its own
scaffolding (tracked separately).

``_fetch_one`` and ``_fetch_all`` use ``dict_row`` cursors (matching the
stores' former ``cursor(row_factory=dict_row)`` scaffolding). ``_execute``
uses the default cursor since it only returns ``rowcount``. ``_transaction``
yields a ``dict_row`` cursor for multi-statement work.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Sequence

from psycopg import Cursor
from psycopg.rows import dict_row

from shared.postgres import get_conn


class PostgresHelperMixin:
    """Mixin exposing fetch-one/fetch-all/execute helpers over ``shared.postgres.get_conn``."""

    def _fetch_one(self, sql: str, params: Sequence[Any] = ()) -> Optional[Dict[str, Any]]:
        """Run ``sql`` and return the first row as a dict, or None if there isn't one."""
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return cur.fetchone()

    def _fetch_all(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        """Run ``sql`` and return all rows as dicts."""
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Run ``sql`` (insert/update/delete) and return the affected row count."""
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.rowcount

    @contextmanager
    def _transaction(self) -> Iterator[Cursor]:
        """Yield a dict-row cursor for statements that must share one transaction.

        ``_fetch_one``/``_fetch_all``/``_execute`` each run a single statement
        on its own connection. Some call sites — a check-then-insert, or a
        ``SELECT ... FOR UPDATE`` guarding the write that follows — need more
        than one statement to commit or roll back together. This opens one
        connection and yields its cursor so the caller can issue multiple
        ``execute()`` calls against it.
        """
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            yield cur
