"""Shared Postgres access helpers for branding_team's store classes.

Not yet consumed by ``store.py``, ``assistant/store.py``, or ``api/state.py``
(that migration is tracked separately) — this module only establishes the
helper. It reproduces the exact ``get_conn()`` / ``cursor(row_factory=dict_row)``
semantics those stores hand-write in every method today, so adopting it later
is a drop-in replacement.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

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
