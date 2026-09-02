"""Offline coverage for the address-comments resolve-attempt ledger (no live
Postgres needed).

Exercises the disabled fast-paths and the best-effort exception handling so the
store's degrade-to-safe-default behaviour is verified even on a run without a
database.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import software_engineering_team.resolve_attempt_store as store


class _FakeCursor:
    def __init__(self, fetchone_result=None) -> None:
        self.fetchone_result = fetchone_result
        self.executed: list[tuple] = []

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *_a) -> bool:
        return False

    def execute(self, query, params=None) -> None:
        self.executed.append((str(query), params))

    def fetchone(self):
        return self.fetchone_result


class _FakeConn:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    def __enter__(self) -> "_FakeConn":
        return self

    def __exit__(self, *_a) -> bool:
        return False

    def cursor(self, *_a, **_kw) -> _FakeCursor:
        return self._cursor


def test_writes_are_noop_when_postgres_disabled(monkeypatch) -> None:
    monkeypatch.setattr(store, "is_postgres_enabled", lambda: False)

    # A MagicMock (rather than a raising stub) proves get_conn was never
    # called via `.assert_not_called()` below — a raising stub only proves it
    # wasn't called IF the store's own best-effort exception handling doesn't
    # swallow the resulting AssertionError, which would make the check
    # vacuous in a regression.
    no_conn = MagicMock(side_effect=AssertionError("get_conn must not be called"))
    monkeypatch.setattr(store, "get_conn", no_conn)
    # None of these touch the database and none raise.
    store.record_resolve_failure("o", "r", 7, "T1", 3)
    store.clear_resolve_attempt("o", "r", 7, "T1")
    store.clear_resolve_attempts_for_pr("o", "r", 7)
    no_conn.assert_not_called()


def test_has_recorded_resolve_failure_false_when_postgres_disabled(monkeypatch) -> None:
    monkeypatch.setattr(store, "is_postgres_enabled", lambda: False)

    no_conn = MagicMock(side_effect=AssertionError("get_conn must not be called"))
    monkeypatch.setattr(store, "get_conn", no_conn)
    # "No evidence" is the safe default — routes the caller away from
    # auto-resolving a possibly reviewer-reopened thread.
    assert store.has_recorded_resolve_failure("o", "r", 7, "T1", 3) is False
    no_conn.assert_not_called()


def test_writes_swallow_db_errors(monkeypatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setattr(store, "is_postgres_enabled", lambda: True)

    def _boom(*_a, **_kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "get_conn", _boom)
    # Best-effort: a DB failure is logged (via shared.postgres.helpers.best_effort_write), never raised.
    with caplog.at_level("WARNING"):
        store.record_resolve_failure("o", "r", 7, "T1", 3)
        store.clear_resolve_attempt("o", "r", 7, "T1")
        store.clear_resolve_attempts_for_pr("o", "r", 7)
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 3
    assert all("failed" in r.getMessage() for r in warnings)


def test_has_recorded_resolve_failure_degrades_on_db_error(monkeypatch) -> None:
    monkeypatch.setattr(store, "is_postgres_enabled", lambda: True)

    def _boom(*_a, **_kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "get_conn", _boom)
    # A DB failure degrades to "no evidence" rather than raising or reporting
    # a false positive that would authorize an unsafe auto-resolve.
    assert store.has_recorded_resolve_failure("o", "r", 7, "T1", 3) is False


def test_has_recorded_resolve_failure_true_when_matching_row_exists(monkeypatch) -> None:
    monkeypatch.setattr(store, "is_postgres_enabled", lambda: True)
    cursor = _FakeCursor(fetchone_result=(1,))
    monkeypatch.setattr(store, "get_conn", lambda: _FakeConn(cursor))

    assert store.has_recorded_resolve_failure("o", "r", 7, "T1", 3) is True
    assert cursor.executed
    _query, params = cursor.executed[0]
    assert params == ("o", "r", 7, "T1", 3)


def test_has_recorded_resolve_failure_false_when_no_row(monkeypatch) -> None:
    monkeypatch.setattr(store, "is_postgres_enabled", lambda: True)
    cursor = _FakeCursor(fetchone_result=None)
    monkeypatch.setattr(store, "get_conn", lambda: _FakeConn(cursor))

    assert store.has_recorded_resolve_failure("o", "r", 7, "T1", 3) is False


def test_has_recorded_resolve_failure_uses_null_safe_comparison(monkeypatch) -> None:
    """P1 regression: a row recorded with `khala_reply_comment_id=None` (the
    reply's id could not be captured) must remain reachable by a read call
    that also passes `None` — plain SQL `=` never matches `NULL` on either
    side, which would otherwise make such a row permanently invisible to
    `has_recorded_resolve_failure`, silently defeating the ledger for that
    thread. The query must use a NULL-safe comparison instead."""
    monkeypatch.setattr(store, "is_postgres_enabled", lambda: True)
    cursor = _FakeCursor(fetchone_result=(1,))
    monkeypatch.setattr(store, "get_conn", lambda: _FakeConn(cursor))

    assert store.has_recorded_resolve_failure("o", "r", 7, "T1", None) is True
    _query, params = cursor.executed[0]
    assert "IS NOT DISTINCT FROM" in _query
    assert params == ("o", "r", 7, "T1", None)


def test_record_resolve_failure_upserts_with_expected_params(monkeypatch) -> None:
    monkeypatch.setattr(store, "is_postgres_enabled", lambda: True)
    cursor = _FakeCursor()
    monkeypatch.setattr(store, "get_conn", lambda: _FakeConn(cursor))

    store.record_resolve_failure("o", "r", 7, "T1", 3)

    assert len(cursor.executed) == 1
    query, params = cursor.executed[0]
    assert "INSERT INTO address_comments_resolve_attempts" in query
    assert "ON CONFLICT" in query
    assert params == ("o", "r", 7, "T1", 3)


def test_clear_resolve_attempt_deletes_with_expected_params(monkeypatch) -> None:
    monkeypatch.setattr(store, "is_postgres_enabled", lambda: True)
    cursor = _FakeCursor()
    monkeypatch.setattr(store, "get_conn", lambda: _FakeConn(cursor))

    store.clear_resolve_attempt("o", "r", 7, "T1")

    assert len(cursor.executed) == 1
    query, params = cursor.executed[0]
    assert "DELETE FROM address_comments_resolve_attempts" in query
    assert "thread_id" in query
    assert params == ("o", "r", 7, "T1")


def test_clear_resolve_attempts_for_pr_deletes_with_expected_params(monkeypatch) -> None:
    monkeypatch.setattr(store, "is_postgres_enabled", lambda: True)
    cursor = _FakeCursor()
    monkeypatch.setattr(store, "get_conn", lambda: _FakeConn(cursor))

    store.clear_resolve_attempts_for_pr("o", "r", 7)

    assert len(cursor.executed) == 1
    query, params = cursor.executed[0]
    assert "DELETE FROM address_comments_resolve_attempts" in query
    assert "thread_id" not in query
    assert params == ("o", "r", 7)


# --- Executable ledger semantics (the store's REAL SQL, replayed on sqlite) ---
#
# The tests above assert on the SQL's SHAPE. The sequence that matters most for
# this ledger -- record WITH a reply id, then record again WITHOUT one, then read
# back with the real id -- is a question about the upsert's CONFLICT semantics,
# which a shape assertion cannot answer. These tests therefore run the store's
# own query strings (captured verbatim from the module, never retyped here, so a
# query change cannot pass unnoticed) against an in-memory sqlite database,
# translating only the three dialect tokens sqlite spells differently.


class _SqliteCursor:
    """Cursor that executes the store's real SQL against sqlite.

    Preconditions:
        - ``conn`` is an open sqlite3 connection whose schema already has the
          ``address_comments_resolve_attempts`` table.
    Postconditions:
        - ``execute`` runs the store's Postgres SQL after rewriting exactly
          three dialect tokens (``%s`` placeholders, ``NOW()``, and the
          ``IS NOT DISTINCT FROM`` null-safe comparison). Nothing about the
          upsert's ``ON CONFLICT``/``COALESCE`` logic -- the behaviour under
          test -- is rewritten.
    """

    def __init__(self, conn) -> None:
        self._conn = conn
        self._cur = conn.cursor()

    def __enter__(self) -> "_SqliteCursor":
        return self

    def __exit__(self, *_a) -> bool:
        self._cur.close()
        return False

    def execute(self, query, params=None) -> None:
        sql = (
            str(query)
            .replace("%s", "?")
            .replace("NOW()", "CURRENT_TIMESTAMP")
            .replace("IS NOT DISTINCT FROM", "IS")
        )
        self._cur.execute(sql, tuple(params or ()))

    def fetchone(self):
        return self._cur.fetchone()


class _SqliteConn:
    def __init__(self, conn) -> None:
        self._conn = conn

    def __enter__(self) -> "_SqliteConn":
        return self

    def __exit__(self, *_a) -> bool:
        self._conn.commit()
        return False

    def cursor(self, *_a, **_kw) -> _SqliteCursor:
        return _SqliteCursor(self._conn)


@pytest.fixture()
def sqlite_store(monkeypatch):
    """Point the store at an in-memory sqlite database with the real table shape."""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE address_comments_resolve_attempts (
               owner TEXT NOT NULL,
               repo TEXT NOT NULL,
               pr_number INTEGER NOT NULL,
               thread_id TEXT NOT NULL,
               khala_reply_comment_id INTEGER,
               failed_at TEXT NOT NULL,
               PRIMARY KEY (owner, repo, pr_number, thread_id)
           )"""
    )
    monkeypatch.setattr(store, "is_postgres_enabled", lambda: True)
    monkeypatch.setattr(store, "get_conn", lambda: _SqliteConn(conn))
    try:
        yield conn
    finally:
        conn.close()


def test_recorded_reply_id_survives_a_later_failure_with_an_unknown_id(sqlite_store) -> None:
    """P1 regression: a second failure recorded with an UNKNOWN reply id must not
    erase the id the first failure recorded.

    An unconditional ``khala_reply_comment_id = EXCLUDED.khala_reply_comment_id``
    nulls the column out here, after which the read below compares NULL against
    the real reply id (``IS NOT DISTINCT FROM``) and returns False -- destroying
    exactly the evidence this ledger exists to preserve and silently routing a
    retryable thread back onto the "possible reviewer reopen, never auto-resolve"
    path. ``COALESCE`` keeps the known id.
    """
    store.record_resolve_failure("o", "r", 7, "T1", 3)
    store.record_resolve_failure("o", "r", 7, "T1", None)

    assert store.has_recorded_resolve_failure("o", "r", 7, "T1", 3) is True
    row = sqlite_store.execute(
        "SELECT khala_reply_comment_id FROM address_comments_resolve_attempts"
    ).fetchone()
    assert row == (3,)


def test_a_newer_known_reply_id_still_supersedes_the_recorded_one(sqlite_store) -> None:
    """COALESCE preserves only across an UNKNOWN (NULL) id: a failure recorded
    against a genuinely newer Khala reply must still overwrite the old id, or the
    ledger would report evidence for a reply that has since been superseded."""
    store.record_resolve_failure("o", "r", 7, "T1", 3)
    store.record_resolve_failure("o", "r", 7, "T1", 9)

    assert store.has_recorded_resolve_failure("o", "r", 7, "T1", 9) is True
    assert store.has_recorded_resolve_failure("o", "r", 7, "T1", 3) is False


def test_a_failure_first_recorded_without_an_id_is_readable_by_a_none_read(
    sqlite_store,
) -> None:
    """The NULL row is not a bug in itself -- it is "we know this failed but not
    which reply it followed" -- and must stay reachable by the symmetric
    ``None`` read (the null-safe comparison), not just by an id read."""
    store.record_resolve_failure("o", "r", 7, "T2", None)

    assert store.has_recorded_resolve_failure("o", "r", 7, "T2", None) is True
    assert store.has_recorded_resolve_failure("o", "r", 7, "T2", 3) is False


def test_clear_removes_the_row_end_to_end(sqlite_store) -> None:
    """The cleanup path deletes the evidence the two writes above created, so a
    resolved thread stops looking like a known failure on the next run."""
    store.record_resolve_failure("o", "r", 7, "T1", 3)
    store.clear_resolve_attempt("o", "r", 7, "T1")

    assert store.has_recorded_resolve_failure("o", "r", 7, "T1", 3) is False
