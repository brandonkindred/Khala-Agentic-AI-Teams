"""Unit tests for ``shared.postgres.fake`` (no live Postgres)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from shared.postgres.fake import FakeConn, FakeCursor, install_fake_postgres, unwrap_json


def test_unwrap_json_plain_value():
    assert unwrap_json({"a": 1}) == {"a": 1}
    assert unwrap_json("x") == "x"
    assert unwrap_json(None) is None


def test_unwrap_json_obj_wrapper():
    wrapped = SimpleNamespace(obj={"k": "v"})
    assert unwrap_json(wrapped) == {"k": "v"}


def _sample_dispatch():
    def match_insert(norm: str) -> bool:
        return norm.startswith("insert into items")

    def handle_insert(cur: FakeCursor, params: tuple) -> None:
        item_id, payload = params
        cur.db.setdefault("items", {})[item_id] = unwrap_json(payload)
        cur.rowcount = 1

    def match_select_one(norm: str) -> bool:
        return norm.startswith("select payload from items where id")

    def handle_select_one(cur: FakeCursor, params: tuple) -> None:
        (item_id,) = params
        row = cur.db.get("items", {}).get(item_id)
        cur.set_one({"payload": row} if row is not None else None)

    def match_select_all(norm: str) -> bool:
        return norm.startswith("select payload from items")

    def handle_select_all(cur: FakeCursor, params: tuple) -> None:
        cur.set_all([{"payload": v} for v in cur.db.get("items", {}).values()])

    def match_next_id(norm: str) -> bool:
        return norm.startswith("insert into notes")

    def handle_next_id(cur: FakeCursor, params: tuple) -> None:
        (text,) = params
        note_id = next(cur.ids)
        cur.db.setdefault("notes", []).append({"id": note_id, "text": text})
        cur.set_one((note_id,))
        cur.rowcount = 1

    return [
        (match_insert, handle_insert),
        (match_select_one, handle_select_one),
        (match_select_all, handle_select_all),
        (match_next_id, handle_next_id),
    ]


def test_first_match_wins_and_fetchone():
    db: dict = {}
    cur = FakeCursor(db, _sample_dispatch())
    cur.execute("INSERT INTO items VALUES (%s, %s)", ("a", SimpleNamespace(obj={"n": 1})))
    assert cur.rowcount == 1
    assert db["items"]["a"] == {"n": 1}

    cur.execute("SELECT payload FROM items WHERE id = %s", ("a",))
    assert cur.fetchone() == {"payload": {"n": 1}}


def test_fetchall_and_whitespace_normalization():
    db = {"items": {"a": 1, "b": 2}}
    cur = FakeCursor(db, _sample_dispatch())
    cur.execute(
        """
        SELECT   payload
        FROM items
        """
    )
    assert cur.fetchall() == [{"payload": 1}, {"payload": 2}]


def test_unmatched_sql_raises_with_original_string():
    cur = FakeCursor({}, _sample_dispatch())
    sql = "DELETE FROM items WHERE id = %s"
    with pytest.raises(AssertionError, match=r"unexpected SQL in fake cursor"):
        cur.execute(sql, ("a",))


def test_shared_ids_across_executes():
    db: dict = {}
    cur = FakeCursor(db, _sample_dispatch())
    cur.execute("INSERT INTO notes VALUES (%s)", ("one",))
    assert cur.fetchone() == (1,)
    cur.execute("INSERT INTO notes VALUES (%s)", ("two",))
    assert cur.fetchone() == (2,)
    assert [n["id"] for n in db["notes"]] == [1, 2]


def test_cursor_context_manager():
    cur = FakeCursor({}, _sample_dispatch())
    with cur as entered:
        assert entered is cur


def test_fake_conn_cursor_shares_db_and_ids():
    db: dict = {}
    conn = FakeConn(db, _sample_dispatch())
    cur_a = conn.cursor()
    cur_b = conn.cursor()

    cur_a.execute("INSERT INTO items VALUES (%s, %s)", ("a", {"n": 1}))
    cur_b.execute("SELECT payload FROM items WHERE id = %s", ("a",))
    assert cur_b.fetchone() == {"payload": {"n": 1}}

    cur_a.execute("INSERT INTO notes VALUES (%s)", ("one",))
    cur_b.execute("INSERT INTO notes VALUES (%s)", ("two",))
    assert cur_a.fetchone() == (1,)
    assert cur_b.fetchone() == (2,)


def test_install_fake_postgres_patches_get_conn(monkeypatch):
    stub = SimpleNamespace(get_conn=None)
    db = install_fake_postgres(
        monkeypatch,
        modules=[stub],
        dispatch=_sample_dispatch(),
        id_start=10,
    )

    with stub.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO notes VALUES (%s)", ("hello",))
            assert cur.fetchone() == (10,)

    assert db is not None
    assert db["notes"][0]["text"] == "hello"
