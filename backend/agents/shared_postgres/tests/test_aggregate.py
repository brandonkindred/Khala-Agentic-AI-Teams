"""Tests for the atomic JSONB-aggregate merge helpers.

The pure statement builder and the inject-a-cursor merge run without a
database (a stub cursor stands in). A final, ``POSTGRES_HOST``-gated test
validates the real ``data || patch`` semantics against live Postgres so the
fakes elsewhere can't drift from the engine.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from uuid import uuid4

import pytest
from pydantic import BaseModel

from shared_postgres.aggregate import (
    build_merge_statement,
    merge_jsonb_returning,
    merge_jsonb_via_cursor,
)


class _Doc(BaseModel):
    a: int = 0
    b: int = 0


class _StubCursor:
    """Records the executed statement/params and returns a canned row.

    Doubles as a context manager so it can stand in for a real
    ``conn.cursor(...)`` in :func:`merge_jsonb_returning`'s ``with`` block.
    """

    def __init__(self, result):
        self._result = result
        self.executed: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params):
        self.executed.append((sql, list(params)))

    def fetchone(self):
        return self._result


def test_build_merge_statement_shape() -> None:
    stmt = build_merge_statement("branding_brands", "data", ["id", "client_id"])
    assert stmt == (
        "UPDATE branding_brands SET data = data || %s::jsonb "
        "WHERE id = %s AND client_id = %s RETURNING data"
    )


def test_build_merge_statement_rejects_bad_identifiers() -> None:
    with pytest.raises(ValueError, match="table"):
        build_merge_statement("brands; DROP TABLE x", "data", ["id"])
    with pytest.raises(ValueError, match="data_column"):
        build_merge_statement("brands", "data->>'x'", ["id"])
    with pytest.raises(ValueError, match="key column"):
        build_merge_statement("brands", "data", ["id = 1 OR 1=1"])
    with pytest.raises(ValueError, match="non-empty"):
        build_merge_statement("brands", "data", [])


def test_merge_via_cursor_returns_merged_dict_and_wraps_patch() -> None:
    cur = _StubCursor({"data": {"a": 1, "b": 2}})
    out = merge_jsonb_via_cursor(
        cur, "branding_brands", key={"id": "b1", "client_id": "c1"}, patch={"b": 2}
    )
    assert out == {"a": 1, "b": 2}
    sql, params = cur.executed[0]
    assert sql.startswith("UPDATE branding_brands SET data = data || %s::jsonb")
    # First param is the psycopg Json-wrapped patch; the rest are key values in order.
    assert params[0].obj == {"b": 2}
    assert params[1:] == ["b1", "c1"]


def test_merge_via_cursor_coerces_model_and_handles_miss() -> None:
    cur = _StubCursor({"data": {"a": 5, "b": 6}})
    doc = merge_jsonb_via_cursor(cur, "t", key={"id": "x"}, patch={"a": 5}, model=_Doc)
    assert isinstance(doc, _Doc) and doc.a == 5 and doc.b == 6

    miss = merge_jsonb_via_cursor(_StubCursor(None), "t", key={"id": "x"}, patch={"a": 1})
    assert miss is None


def test_merge_via_cursor_rejects_empty_key() -> None:
    with pytest.raises(ValueError, match="at least one column"):
        merge_jsonb_via_cursor(_StubCursor(None), "t", key={}, patch={"a": 1})


def test_merge_jsonb_returning_drives_connection(monkeypatch) -> None:
    """Cover the get_conn() path of merge_jsonb_returning without a live DB."""
    import shared_postgres.aggregate as agg

    cur = _StubCursor({"data": {"x": 1, "y": 2}})

    class _Conn:
        def cursor(self, row_factory=None):
            return cur

    @contextmanager
    def _fake_get_conn(database=None):
        yield _Conn()

    monkeypatch.setattr(agg, "get_conn", _fake_get_conn)
    out = agg.merge_jsonb_returning("branding_brands", key={"id": "r1"}, patch={"y": 2})
    assert out == {"x": 1, "y": 2}
    sql, params = cur.executed[0]
    assert sql.endswith("RETURNING data")
    assert params[1:] == ["r1"]


@pytest.mark.skipif(
    not os.environ.get("POSTGRES_HOST", "").strip(),
    reason="POSTGRES_HOST not set; skipping live Postgres semantics check",
)
def test_merge_jsonb_returning_against_live_postgres() -> None:
    from shared_postgres import get_conn

    table = f"_agg_merge_test_{uuid4().hex[:8]}"
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(f"CREATE TABLE {table} (id TEXT PRIMARY KEY, data JSONB NOT NULL)")
            cur.execute(
                f"INSERT INTO {table} (id, data) VALUES (%s, %s::jsonb)",
                ("r1", '{"keep": 1, "name": "old"}'),
            )
        merged = merge_jsonb_returning(
            table, key={"id": "r1"}, patch={"name": "new", "extra": True}
        )
        # Top-level merge: patched keys replace, untouched keys survive.
        assert merged == {"keep": 1, "name": "new", "extra": True}
        # A non-existent key returns None and changes nothing.
        assert merge_jsonb_returning(table, key={"id": "missing"}, patch={"x": 1}) is None
    finally:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {table}")
