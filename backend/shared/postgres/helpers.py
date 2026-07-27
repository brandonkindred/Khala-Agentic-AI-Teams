"""Shared fetch/execute helpers for team stores, built on ``pg_cursor``.

Promotes the ``PostgresHelperMixin`` pattern originally implemented
team-locally in ``branding_team/_db.py`` (and duplicated ad hoc, unfactored,
in ``investment_team/market_data_cache/store.py``) into ``shared.postgres``,
so a team store gets ``_fetch_one``/``_fetch_all``/``_execute``/
``_transaction`` without reimplementing connection/cursor acquisition.

Built on :func:`shared.postgres.client.pg_cursor` rather than ``get_conn`` +
``conn.cursor()`` directly, so the ``is_postgres_enabled()`` guard and
cursor-acquisition logic stay centralized in one place instead of being
copied into every mixin implementation.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Sequence

from shared.postgres.client import pg_cursor

_DISABLED_MSG = "POSTGRES_HOST is not set; cannot open a Postgres connection."


class PostgresHelperMixin:
    """Mixin exposing fetch-one/fetch-all/execute/transaction helpers over ``pg_cursor``."""

    def _fetch_one(self, sql: str, params: Sequence[Any] = ()) -> Optional[Dict[str, Any]]:
        """Run ``sql`` and return the first row as a dict, or None if there isn't one.

        Preconditions:
            ``sql`` is a non-empty SQL query string.
            ``params`` is a sequence matching the statement's ``%s`` placeholders.
        Postconditions:
            Returns a ``dict`` keyed by column name when a row exists, else ``None``.
            Raises ``RuntimeError`` when Postgres is disabled (``POSTGRES_HOST`` unset).
            Propagates ``psycopg.Error`` (and subclasses) on connection or query failure.
        """
        with pg_cursor(dict_rows=True) as cur:
            if cur is None:
                raise RuntimeError(_DISABLED_MSG)
            cur.execute(sql, params)
            return cur.fetchone()

    def _fetch_all(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        """Run ``sql`` and return all rows as dicts.

        Preconditions:
            ``sql`` is a non-empty SQL query string.
            ``params`` is a sequence matching the statement's ``%s`` placeholders.
        Postconditions:
            Returns a list of column-name-keyed dicts (empty when no rows match).
            Raises ``RuntimeError`` when Postgres is disabled (``POSTGRES_HOST`` unset).
            Propagates ``psycopg.Error`` (and subclasses) on connection or query failure.
        """
        with pg_cursor(dict_rows=True) as cur:
            if cur is None:
                raise RuntimeError(_DISABLED_MSG)
            cur.execute(sql, params)
            return cur.fetchall()

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Run ``sql`` (insert/update/delete) and return the affected row count.

        Preconditions:
            ``sql`` is a non-empty SQL statement string.
            ``params`` is a sequence matching the statement's ``%s`` placeholders.
        Postconditions:
            Returns the number of rows affected (``cursor.rowcount``).
            Raises ``RuntimeError`` when Postgres is disabled (``POSTGRES_HOST`` unset).
            Propagates ``psycopg.Error`` (and subclasses) on connection or query failure.
        """
        with pg_cursor(dict_rows=False) as cur:
            if cur is None:
                raise RuntimeError(_DISABLED_MSG)
            cur.execute(sql, params)
            return cur.rowcount

    @contextmanager
    def _transaction(self) -> Iterator[Any]:
        """Yield a dict-row cursor for statements that must share one transaction.

        ``_fetch_one``/``_fetch_all``/``_execute`` each run a single statement
        on its own connection. Some call sites — a check-then-insert, or a
        ``SELECT ... FOR UPDATE`` guarding the write that follows — need more
        than one statement to commit or roll back together. This opens one
        connection (via ``pg_cursor``) and yields its cursor so the caller can
        issue multiple ``execute()`` calls against it.

        Preconditions:
            Caller uses the yielded cursor only inside the ``with`` block and
            does not retain it after the context exits.
        Postconditions:
            Yields a ``dict_row`` cursor bound to one connection/transaction.
            On clean exit the connection context commits; on exception it
            rolls back. Raises ``RuntimeError`` when Postgres is disabled
            (``POSTGRES_HOST`` unset). Propagates ``psycopg.Error`` (and
            subclasses) raised while acquiring the connection or while the
            caller uses the cursor.
        """
        with pg_cursor(dict_rows=True) as cur:
            if cur is None:
                raise RuntimeError(_DISABLED_MSG)
            yield cur
