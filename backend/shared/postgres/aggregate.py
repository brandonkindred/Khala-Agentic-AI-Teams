"""Atomic JSONB-aggregate update helpers for whole-document stores.

Several teams persist an entire aggregate (a Pydantic model) in a single
JSONB column. The naive update path is *read-modify-write*: ``SELECT`` the
row, ``model_validate`` the whole document, mutate it in Python, then rewrite
the whole blob. That costs two round-trips, a full deserialize + reserialize
of fields that did not change, and it races with concurrent writers — last
write wins, silently clobbering a concurrent change.

:func:`merge_jsonb_returning` (and its inject-a-cursor sibling
:func:`merge_jsonb_via_cursor`) replace that with a single atomic statement
that shallow-merges a patch into the stored document server-side
(``data = data || patch``) and returns the merged document. One round-trip,
no Python-side (de)serialize of the unchanged fields, and concurrency-safe:
each writer's top-level keys land atomically.

Identifier safety:
    ``table`` / ``data_column`` / key-column names are validated as plain SQL
    identifiers and interpolated into the statement. They are *trusted code
    literals*, never request input — the validation is defense-in-depth, not a
    sanitization boundary for attacker-controlled names.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional

from shared_postgres.client import get_conn

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_identifier(name: str, *, what: str) -> str:
    """Return ``name`` if it is a bare SQL identifier, else raise ``ValueError``.

    Preconditions:
        - ``name`` is a trusted code literal (table/column name), not user input.
    Postconditions:
        - Returns ``name`` unchanged when it matches ``[A-Za-z_][A-Za-z0-9_]*``.
        - Raises ``ValueError`` otherwise (never returns an unsafe identifier).
    """
    if not isinstance(name, str) or not _IDENT_RE.match(name):
        raise ValueError(f"invalid {what} identifier: {name!r}")
    return name


def build_merge_statement(table: str, data_column: str, key_columns: list[str]) -> str:
    """Build the parameterized ``UPDATE ... RETURNING`` merge statement (pure).

    Preconditions:
        - ``table`` and ``data_column`` are valid SQL identifiers.
        - ``key_columns`` is non-empty; each entry is a valid SQL identifier.
    Postconditions:
        - Returns a statement with exactly one ``%s`` placeholder for the JSONB
          patch, followed by one ``%s`` per key column in ``key_columns`` order,
          and a ``RETURNING <data_column>`` clause.
        - The statement performs a shallow top-level merge (``data || patch``);
          keys present in the patch replace those in the stored document.
    """
    _validate_identifier(table, what="table")
    _validate_identifier(data_column, what="data_column")
    if not key_columns:
        raise ValueError("key_columns must be non-empty")
    for col in key_columns:
        _validate_identifier(col, what="key column")
    where = " AND ".join(f"{c} = %s" for c in key_columns)
    return (
        f"UPDATE {table} SET {data_column} = {data_column} || %s::jsonb "
        f"WHERE {where} RETURNING {data_column}"
    )


def merge_jsonb_via_cursor(
    cur: Any,
    table: str,
    *,
    key: Mapping[str, Any],
    patch: Mapping[str, Any],
    data_column: str = "data",
    model: Optional[Any] = None,
) -> Any:
    """Atomically shallow-merge ``patch`` into a row's JSONB document via ``cur``.

    Use this when the caller already holds an open cursor (e.g. a store that
    manages its own pooled connection so a test double can intercept it). The
    cursor must yield mapping rows (``row_factory=dict_row``) so the merged
    document is reachable as ``row[data_column]``.

    Preconditions:
        - ``cur`` is an open DB-API cursor returning mapping rows.
        - ``key`` is non-empty; every value is a column predicate (``col = %s``).
        - ``patch`` values are JSON-serialisable (``str``/``int``/``float``/
          ``bool``/``None``/``list``/``dict``) — enums/models must be dumped by
          the caller first.
    Postconditions:
        - Issues exactly one ``UPDATE ... RETURNING`` statement.
        - Returns the merged document as a ``dict`` (or ``model.model_validate``
          of it when ``model`` is given), or ``None`` when no row matched ``key``.
    """
    from psycopg.types.json import Json  # noqa: PLC0415 — keep psycopg optional at import

    if not key:
        raise ValueError("key must map at least one column to a value")
    key_columns = list(key.keys())
    stmt = build_merge_statement(table, data_column, key_columns)
    params = [Json(dict(patch)), *(key[c] for c in key_columns)]
    cur.execute(stmt, params)
    row = cur.fetchone()
    if row is None:
        return None
    doc = row[data_column]
    return model.model_validate(doc) if model is not None else doc


def merge_jsonb_returning(
    table: str,
    *,
    key: Mapping[str, Any],
    patch: Mapping[str, Any],
    data_column: str = "data",
    database: Optional[str] = None,
    model: Optional[Any] = None,
) -> Any:
    """Open a pooled connection and atomically merge ``patch`` into one row.

    Self-contained convenience wrapper over :func:`merge_jsonb_via_cursor` for
    stores that do not need to share a transaction with surrounding work.

    Preconditions / Postconditions: as :func:`merge_jsonb_via_cursor`, plus the
    write is committed when the ``get_conn`` context exits cleanly.
    """
    from psycopg.rows import dict_row  # noqa: PLC0415 — keep psycopg optional at import

    with get_conn(database) as conn, conn.cursor(row_factory=dict_row) as cur:
        return merge_jsonb_via_cursor(
            cur, table, key=key, patch=patch, data_column=data_column, model=model
        )
